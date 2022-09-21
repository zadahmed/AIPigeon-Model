import jwt
import time
import os
from redis import Redis
import time
from datetime import timedelta
from jproperties import Properties

configs = Properties()

with open('./config/config-' + os.environ["environment"] + '.properties', 'rb') as config_file:
    configs.load(config_file)

JWT_SECRET = configs['SALT_KEY'].data
JWT_ENC_ALGORITHM = configs['JWT_ENC_ALGORITHM'].data
API_URL = configs["API_URL"].data
REDIS_PASS = configs["REDIS_PASS"].data
REDIS_PORT = configs["REDIS_PORT"].data

redis_conn = Redis(host=API_URL, port=REDIS_PORT, db=0, decode_responses=True, password=REDIS_PASS)


def signJWT(id: str, jwt_token_expiry: int):
    payload = {
        "userid": id,
        "exp": time.time() + int(jwt_token_expiry),
        "iss": "aipigeon"
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ENC_ALGORITHM)
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def decodeJWT(token: str):
    try:
        # validate if the token present in blacklisted token
        entry = redis_conn.get(token)
        if entry and entry == 'true':
            return {}
        else:
            decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ENC_ALGORITHM])
            return decoded_token if decoded_token["exp"] >= time.time() else None
    except:
        return {}


def revokeJWT(token: str):
    try:
        redis_conn.setex(token, timedelta(days=1), 'true')
        return True
    except:
        return False
