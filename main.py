import asyncio
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import time
import zipfile

import fastapi
import pymongo
import requests
from fastapi import FastAPI, Response, Request, WebSocket, status, Depends, WebSocketDisconnect, BackgroundTasks
from pymongo import MongoClient
from bson import ObjectId
from bson.json_util import dumps
import uvicorn
import os
import pytz
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from jproperties import Properties
from auth.auth_handler import signJWT, decodeJWT, revokeJWT
from auth.auth_bearer import JWTBearer
import random
from os import path
import docker
import uuid
import copy
import shutil
import io
import traceback
import re
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()

configs = Properties()

with open('./config/config-' + os.environ["environment"] + '.properties', 'rb') as config_file:
    configs.load(config_file)

logging.basicConfig(
    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
    handlers=[TimedRotatingFileHandler('./logs/train.log', when='midnight', backupCount=60)],
)
log = logging.getLogger(__name__)

MONGO_DB_URL = "mongodb://" + os.environ["DB_USERNAME"] + ":" + os.environ["DB_PASSWORD"] + "@" + os.environ["DB_CONN_URL"] + "/?authSource=admin"
MONGO_DB_NAME = os.environ["DB_NAME"]
COLLECTION_INTENT_DATA = configs['COLLECTION_INTENT_DATA'].data
RASA_URL = configs['RASA_URL'].data
RASA_HOME_DIR = configs['RASA_HOME_DIR'].data
COLLECTION_PORTMAPPER = configs ['COLLECTION_PORTMAPPER'].data
COLLECTION_STORIES = configs['COLLECTION_STORIES'].data
COLLECTION_TRAINING_HISTORY = configs["COLLECTION_TRAINING_HISTORY"].data
COLLECTION_CATEGORIES = configs["COLLECTION_CATEGORIES"].data
COLLECTION_TRAIN_SCHEDULAR = configs["COLLECTION_TRAIN_SCHEDULAR"].data
COLLECTION_TRAINING_LOGS = configs["COLLECTION_TRAINING_LOGS"].data
HOST_PATH = os.environ['HOST_PATH']
origins = [
    "*",
]

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

@app.on_event("startup")
async def startup_event():
    log.info("application startup method invoked")
    global mongoclient
    mongoclient = MongoClient(MONGO_DB_URL, maxPoolSize=1000, minPoolSize=100)
    global db
    db = mongoclient[MONGO_DB_NAME]
    global scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_train_schedule, 'interval', minutes=1)
    scheduler.start()
    log.info("successfully started schedular instance!")
    train_rasa_background_job()

@app.on_event("shutdown")
async def shutdown_event():
    log.info("application shutdown method invoked")
    mongoclient.close()
    log.info("succesfully closed mongodb connection!")
    scheduler.shutdown()
    log.info("succesfully stopped scheduler instance!")


def check_train_schedule():
    try:
        log.info("check_train_Schedule is started-->")
        # validate the train schedular if there is any training jobs to be triggered
        collection_schedular = db[COLLECTION_TRAIN_SCHEDULAR]
        current_server_time = datetime.now()
        current_serverend_time = datetime.now() + timedelta(minutes=5)
        training_data = collection_schedular.find({"next_execution_time": {"$gte": current_server_time, "$lt": current_serverend_time}})
        data = list(training_data)
        log.info("length of db output:-> %d" % len(data))
        for traindata in data:
            orgid = traindata.get("orgid")
            category = traindata.get("category")
            userid = traindata.get("userid")
            collection_training_history = db[COLLECTION_TRAINING_HISTORY]
            count = collection_training_history.count_documents({"orgid": orgid, "category": category,
                                                                 "status":{"$in": ["Inprogress", "Requested"]}})
            log.info("count:-> %d" % count)
            if count > 0:
                log.info("skipping..already an active training request exist for orgid: %s, category: %s" %(orgid, category))
                continue
            else:
                collection_training_history.insert_one({"orgid": orgid, "category": category,
                                                        "status": "Requested", "date": datetime.now(), "userid": "scheduled"})
                # delete the entry in schedular table if the frequency is 1
                scheduleid= traindata.get("_id")
                frequency = traindata.get("frequency")
                next_execution_time_from_db = traindata.get("next_execution_time")
                log.info("frequency-> %s and next_execution_time_from_db: %s" % (frequency, str(next_execution_time_from_db)))
                if frequency == "O":
                    collection_schedular.delete_one({"_id": scheduleid})
                    continue
                elif frequency == "D":
                    next_execution_time = traindata.get("next_execution_time") + timedelta(days= 1)
                elif frequency == "W":
                    next_execution_time = traindata.get("next_execution_time") + timedelta(days= 7)
                elif frequency == "M":
                    next_execution_time = traindata.get("next_execution_time") + timedelta(days= 30)
                log.info("next_execution_time ->> will be updated in DB: %s" % str(next_execution_time))
                collection_schedular.update_one({"_id": scheduleid}, {"$set": {"next_execution_time": next_execution_time}})

    except Exception as e:
        log.error("check_train_Schedule exception block --> %s" , str(e))
    finally:
        log.info("check_train_Schedule is ended-->")


def getLoggerObject(name, category):
    # check if directory exists with the ord id. if not create new directory
    log_directory = './logs/' + name + "/" + category
    if not path.exists(log_directory):
        if not path.exists("./logs/" + name):
            os.mkdir("./logs/" + name)
        os.mkdir("./logs/" + name + "/" + category)
    logger = logging.getLogger(name + "-" + category)
    if len(logger.handlers) == 0:
        handler = TimedRotatingFileHandler(log_directory + "/train.log", when='midnight', backupCount=60)
        formatter = logging.Formatter('%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s','%Y-%m-%d %H:%M:%S')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def train_rasa_background_job():

    while True:
        try:
            os.chdir("/app")
            orgid = "default"
            log_org = getLoggerObject(orgid, "main")
            #check the pending training request:
            collection_training_history = db[COLLECTION_TRAINING_HISTORY]
            pending_training_data = collection_training_history.find({"status": "Requested"}).sort("date", pymongo.ASCENDING).limit(1)
            data = list(pending_training_data)
            if len(data) == 0:
                log_org.info("/train/executejobs -> no pending tasks for training. Sleeping for 60 secs")
                time.sleep(60)
                continue
            for train in data:
                orgid  = train.get("orgid")
                category = train.get("category")
                userid = train.get("userid")
                requestid = train.get("_id")

            log_org = getLoggerObject(orgid, category)
            log_org.info("current working directory: %s " % str(os.getcwd()))
            log_org.info("/train/rasa/%s/%s/%s -> started " % (orgid, category, userid))

            # mark the training request as in progress
            collection_training_history = db[COLLECTION_TRAINING_HISTORY]
            collection_training_history.update_one({"_id": requestid},{"$set": {"status": "Inprogress", "percentage": "0", "train_start_time": datetime.now()}})

            # get port and language pertain to org and category details
            collection_portmapper = db[COLLECTION_PORTMAPPER]
            data = collection_portmapper.find_one({"orgid": orgid, "category": category, "status": "A"})
            if not data:
                log_org.error("/train/rasa/%s/%s/%s-> no port mapped for category: %s and orgid: %s" %(orgid, category, userid, category, orgid))
            port = str(data.get("port"))
            language = data.get("language")
            category_english = data.get("category_english")
            log_org.info("/train/rasa/%s/%s/%s-> port:%s and language: %s, category_english: %s" % (orgid, category, userid, port, language, category_english))

            collection = db[COLLECTION_INTENT_DATA]
            # step1: fetch latest intent data from DB to prepare nlu file
            log_org.info(
                "/train/rasa/%s/%s/%s -> step1: fetch latest intent data from DB to prepare nlu file started" % (orgid, category, userid))
            # if category == 'HHpoker':
            #     data = collection.find({"orgid": orgid, "category": category, "intent_status": {"$ne": "D"}}).skip(0).limit(800)
            # else:
            data = collection.find({"orgid": orgid, "category": category, "intent_status": {"$ne": "D"}})
            # check if diretory exists. if not create the directory for specific game.
            base_directory = './aipigeonsaasdata/' + orgid + "/" + category_english
            if not path.exists(base_directory):
                log_org.info("/train/rasa/%s/%s/%s -> inside folder data folder doesn't exist" % (orgid, category, userid))
                if not path.exists('./aipigeonsaasdata/' + orgid):
                    os.mkdir('./aipigeonsaasdata/' + orgid)
                os.mkdir(base_directory)
                os.mkdir(base_directory + "/data")
                os.mkdir(base_directory + "/domain")
                os.mkdir(base_directory + "/model")

            os.chdir(base_directory)
            # os.system("sudo chmod 777 model")
            log_org.info("current working directory: %s " % str(os.getcwd()))

            #### create domain.yml file
            domain_file_name = "domain.yml"
            domain_intent_list = []
            intent_list = []
            with open("domain/" + domain_file_name, "w", encoding="utf-8") as file3:
                file3.write('version: "2.0"' + "\n\n")
                file3.write("intents:\n")
                for res in data:
                    intent = res.get("intent")
                    # clean special characters at starting index
                    intent_filtered = re.sub(r"^\W+", "", intent).strip() #.replace(" ", "-")
                    if intent_filtered in intent_list: continue
                    if isinstance(intent_filtered, int):
                        intent_filtered = intent_filtered + "."
                    file3.write("  - " + str(intent_filtered) + "\n")
                    intent_list.append(intent_filtered)
                    domain_intent_list.append(intent_filtered)
                # file3.write("\n")
                file3.write("responses:\n")

                # rewind cursor
                data.rewind()
                intent_list = []
                for utter in data:
                    intent = utter.get("intent")
                    # clean special characters at starting index
                    intent_filtered = re.sub(r"^\W+", "", intent).strip() #.replace(" ", "-")
                    if intent_filtered in intent_list: continue
                    if intent_filtered not in domain_intent_list:continue
                    file3.write("  utter_" + intent_filtered + ":\n")

                    answer = utter.get('answer')
                    for ans in answer:
                        answer_filtered = re.sub(r"^\W+", "", ans).strip() #.replace(" ", "-")
                        if isinstance(answer_filtered, int):
                            answer_filtered = answer_filtered + "."
                        if answer_filtered == "":
                            answer_filtered = "."
                        file3.write("    - text: " + answer_filtered + "\n")
                    intent_list.append(intent_filtered)
                file3.write("\n")
            # update the training percentage status
            collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "20"}})

            ## create nlu file
            nlu_file_name = "nlu.yml"
            intent_list = []
            with open("data/" + nlu_file_name, "w", encoding="utf-8") as file1:
                file1.write('version: "2.0"' + "\n\n")
                file1.write("nlu:\n")
                # rewind cursor
                data.rewind()
                for intent_data in data:
                    # writing question to  yml file
                    intent = intent_data.get('intent')
                    # clean special characters at starting index
                    intent_filtered = re.sub(r"^\W+", "", intent).strip()  #.replace(" ", "-")
                    if intent_filtered in intent_list: continue
                    if intent_filtered not in domain_intent_list:continue
                    file1.write("- intent: " + intent_filtered + "\n")
                    file1.write("  examples: |\n")
                    if isinstance(intent_data.get('question'), list):
                        for q in intent_data.get('question'):
                            file1.write("    - " + q + "\n")
                    else:
                        file1.write("\t- " + q + "\n")
                    file1.write("\n")
                    intent_list.append(intent_filtered)

            # update the training percentage status
            collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "30"}})

            # writing stories to yml file
            # excluding stories for HHpoker game to test accuracy
            if category != "HHpoker":
                intent_list = []
                collection_stories = db[COLLECTION_STORIES]
                storydata = collection_stories.find({"orgid": orgid, "category": category, "status": {"$ne": "D"}})
                story_file_name = "stories.yml"
                if storydata.count() != 0:
                    log_org.info("/train/rasa/%s/%s/%s -> inside stories count > 0. Actual count is: %d" % (orgid, category, userid, storydata.count()))
                    with open("data/" + story_file_name, "w", encoding="utf-8") as file2:
                        file2.write('version: "2.0"' + "\n\n")
                        file2.write("stories:\n")
                        for story in storydata:
                            counter = 0
                            stories = story.get('data')
                            for s in stories:
                                intent = s.get("intent")
                                # clean special characters at starting index
                                intent_filtered = re.sub(r"^\W+", "", intent).strip().replace(" ", "-")
                                if intent_filtered not in domain_intent_list:continue
                                if counter == 0:
                                    file2.write("- story: " + story.get("name") + "\n")
                                    file2.write("  steps:\n")
                                    counter = 1
                                file2.write("  - intent: " + intent_filtered + "\n")
                                file2.write("  - action: utter_" + intent_filtered + "\n")
                                intent_list.append(intent_filtered)
                            file2.write("\n")
                else:
                    log_org.info("/train/rasa/%s/%s/%s -> there are no stories present for this game" % (orgid, category, userid))
                    if path.exists(base_directory + "/data/" + story_file_name):
                        os.remove(data_directory + "/data/" + story_file_name)
            else:
                log_org.info("skipping story collection for HHpoker game!!")

            # update the training percentage status
            collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "50"}})

            log_org.info("/train/rasa/%s/%s/%s --> step1: fetch latest intent data from DB to prepare nlu file ended " % (orgid, category, userid))

            os.chdir("/app")
            # step 2 to kick off rasa train
            log_org.info("/train/rasa/%s/%s/%s --> train-> step2: rasa train step started" % (orgid, category, userid))
            container_name = "rasa-train-container-" + os.environ["environment"] + "-" + orgid +"-" + category_english + "-" + port
            client = docker.from_env()
            command = "train --domain " + base_directory + "/domain/" + domain_file_name + " -c config/config-" + language + ".yml --data " + base_directory + "/data/  --out " + base_directory + "/model --fixed-model-name " + category_english + " -v"
            log_org.info("/train/rasa/%s/%s/%s --> RASA command: %s" % (orgid, category, userid, command))
            try:
                containerid = client.containers.get(container_name)
                containerid.stop()
                containerid.remove()
                log_org.info("/train/rasa/%s/%s/%s --> succesfully stopped and removed the old train container" % (orgid, category, userid))
            except:
                log_org.warning("/train/rasa/%s/%s/%s --> train container is not running so unable to stop and remove" % (orgid, category, userid))

            container = client.containers.run(image="rasa/rasa:2.8.7-full", name=container_name, ports={'5005/tcp': None}, user='root',
                                              volumes = {HOST_PATH: {'bind': '/app', 'mode': 'rw'}},
                                              command=command , detach=True)
            log_org.info("containerid: %s logs:->" % container.id)
            # log_org.info(list(container.logs(stream=True, follow= True)))
            train_status = "Failed"
            log_list = []
            collection_train_logs = db[COLLECTION_TRAINING_LOGS]
            for line in container.logs(stream=True):
                log_text = line.strip().decode("utf-8")
                log_list.append(log_text)
                log_org.info(log_text)
                collection_train_logs.find_one_and_update({"trainingid": str(requestid)}, {"$set": {"logs": log_list}}, upsert= True)
                if "Your Rasa model is trained and " in log_text:
                    train_status = "success"

            log_org.info("/train/rasa/%s/%s/%s --> step2: rasa train step ended for category: %s, train_status: %s" % (orgid, category, userid, category, train_status))

            # update the train_status in DB
            if train_status == "Failed":
                log_org.info("/train/rasa/%s/%s/%s --> the training request is Failed so skipping the loop" % (orgid, category, userid))
                collection_training_history.update_one({"_id": requestid},{"$set": {"status": "Failed", "train_end_time": datetime.now()}})
                time.sleep(10)
                continue

            train_data = collection_training_history.find_one({"_id": requestid})
            status = train_data.get("status")
            if status == "Cancelled":
                log_org.info("/train/rasa/%s/%s/%s --> the training request is cancelled so skipping the loop" % (orgid, category, userid))
                time.sleep(10)
                continue
            #update the training percentage status
            collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "80"}})

            # step 3: reload rasa server with latest model info.
            log_org.info("/train/rasa/%s/%s/%s -> step3: reload running rasa server step started for category: %s" % (orgid, category, userid, category))
            new_rasa_url = RASA_URL + ":" + port + "/model"
            log_org.info("/train/rasa/%s/%s/%s -> rasa reload url: %s " % (orgid, category, userid, new_rasa_url))

            try:
                response = requests.put(new_rasa_url, json={"model_file": base_directory+ "/model/" + category_english + ".tar.gz"})
                log_org.info("/train/rasa/%s/%s/%s -> response from rasa reload model api: %d " % (orgid, category, userid, response.status_code))
                log_org.info("/train/rasa/%s/%s/%s > step3: reload running rasa server step ended" %  (orgid, category, userid))
                if response.status_code != 204:
                    log_org.info("/train/rasa/%s/%s/%s --> the training request is failed to  load rasa server so skipping the loop" % (orgid, category, userid))
                    collection_training_history.update_one({"_id": requestid},{"$set": {"status": "Failed", "train_end_time": datetime.now()}})
                else:
                    collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "100", "status": "Completed", "train_end_time": datetime.now()}})

            except:
                log_org.warning(
                    "It seems the rasa model server is not running / this is the first time we are going to run the server")
                # run rasa model server -> this block will be executed for the first time
                run_container_name = "rasa-parse-container" + os.environ["environment"] + "-" + orgid + "-" + category_english + "-" + port
                log_org.info("/train/rasa/%s/%s/%s -> run container name:-> %s" %  (orgid, category, userid, run_container_name))
                run_command = "run --port " + port + " --enable-api  --model " + base_directory + "/model/" + category_english +".tar.gz --endpoints endpoints.yml -vv"
                log_org.info("/train/rasa/%s/%s/%s -> run_command:->%s" % (orgid, category, userid,run_command))
                try:
                    containerid = client.containers.get(run_container_name)
                    containerid.stop()
                    containerid.remove()
                except:
                    log_org.info("/train/rasa/%s/%s/%s -> parse container is not running so it is not stopped" % (orgid, category, userid) )

                run_container = client.containers.run(image="rasa/rasa:2.8.7-full", name=run_container_name, user='root',
                                                      volumes = {HOST_PATH: {'bind': '/app', 'mode': 'rw'}},
                                                      command=run_command, detach=True, ports={port + "/tcp": int(port)})
                log_org.info("/train/rasa/%s/%s/%s > run_containerid: %s logs:->" % (orgid, category, userid,run_container.id))
                # update the training percentage status
                collection_training_history.update_one({"_id": requestid},{"$set": {"percentage": "100", "status": "Completed", "train_end_time": datetime.now()}})

        except ValueError as ve:
            log_org.error("/train/rasa/%s/%s/%s -> value error occured" % (orgid, category, userid))
            collection_training_history.update_one({"_id": requestid},{"$set": {"status": "Failed", "train_end_time": datetime.now()}})
            print(traceback.format_exc())
            time.sleep(60)
        except Exception as e:
            log_org.error("/train/rasa/%s/%s/%s-> Exception occured" % (orgid, category, userid))
            collection_training_history.update_one({"_id": requestid},{"$set": {"status": "Failed", "train_end_time": datetime.now()}})
            print(traceback.format_exc())
            time.sleep(60)
        finally:
            if orgid != "default":
                log_org.info("/train/rasa/%s/%s/%s --> ended" % (orgid, category, userid))
            else:
                log_org.info("/train/executejobs ->  ended -- no tasks")


#
# @app.delete("/train/reloadmodel/{orgid}/{userid}")
# async def cancel_runnin_training(trainingid: str, userid: str):
#     try:
#         log.info("/train/reloadmodel/%s/%s -> started" % (trainingid, userid))
#         collection = db[COLLECTION_TRAINING_HISTORY]
#         data = collection.find_one({"_id": ObjectId(trainingid), "status": "Inprogress"})
#         if not data:
#             return Response(content=dumps({"msg": "No data exists"}), status_code= status.HTTP_401_UNAUTHORIZED, media_type= "application/json")
#         category = data.get("category")
#         orgid = data.get("orgid")
#
#         collection_portmapper = db[COLLECTION_PORTMAPPER]
#         portmapper_data = collection_portmapper.find_one({"orgid": orgid, "category": category, "status": "A"})
#         if not portmapper_data:
#             return Response(content=dumps({"msg": "No data exists"}), status_code= status.HTTP_401_UNAUTHORIZED, media_type= "application/json")
#         category_english = portmapper_data.get("category_english")
#         port = portmapper_data.get("port")
#         container_name = "rasa-train-container-" + os.environ["environment"] + "-" + orgid +"-" + category_english + "-" + port
#         try:
#             client = docker.from_env()
#             containerid = client.containers.get(container_name)
#             log.info("/train/canceltraining/%s/%s -> container id: %s" %(trainingid, userid, str(containerid.id)))
#             containerid.stop()
#             containerid.remove()
#             log.info("/train/canceltraining/%s/%s -> succesfully stopped and removed the train container" % (trainingid, userid))
#         except:
#             log.info("/train/canceltraining/%s/%s -> unable to remove the train container. It seems not running" % (trainingid, userid))
#
#         collection.update_one({"_id": ObjectId(trainingid)}, {"$set": {"status": "Cancelled", "percentage": "0", "train_end_time": datetime.now()}})
#     except Exception as e:
#         log_org.error("/train/canceltraining/%s/%s -> Exception occured" % (trainingid, userid, str(e)))
#         return Response(content=dumps({"msg": "Technical exception occured"}), status_code= status.HTTP_500_INTERNAL_SERVER_ERROR, media_type= "application/json")
#         print(traceback.format_exc())
#     finally:
#         log.info("/train/canceltraining/%s/%s -> ended" % (trainingid, userid))



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=6005, reload=True )
    # uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
