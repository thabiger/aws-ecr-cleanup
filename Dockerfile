FROM python:3.10-alpine

LABEL image.authors="tomasz.habiger@gmail.com"

COPY requirements-docker.txt requirements-docker.txt

RUN pip install -r requirements-docker.txt

ENTRYPOINT ["aws-ecr-cleanup"]