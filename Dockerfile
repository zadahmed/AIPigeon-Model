FROM python:3.7-slim
ENV PYTHONUNBUFFERED 1 

RUN apt-get update && apt-get install -y libssl-dev
RUN  apt-get install  -y gcc libpq-dev python3-dev python3-pip python3-venv python3-wheel curl git libevent-dev
RUN apt-get update && apt-get install -y libzbar-dev
RUN apt-get install -y libfreetype6-dev gfortran python-dev libopenblas-dev liblapack-dev  libxslt-dev libffi-dev
RUN apt-get -y upgrade
ENV TZ=Asia/Dubai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
EXPOSE 5000
WORKDIR /app
ENV environment = xxxx
ENV DB_USERNAME=xxxx
ENV DB_PASSWORD=xxxx
ENV DB_CONN_URL=xxxx
ENV DB_NAME=xxxx
COPY ./requirements.txt .
COPY . .
RUN pip3 install fastapi-utils
RUN pip install -r requirements.txt
CMD ["uvicorn", "--host", "0.0.0.0", "--port", "5000", "main:app"]
