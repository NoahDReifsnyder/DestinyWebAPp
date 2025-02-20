# This is the start of a Dockerfile

# To pull in a base image, uncomment the next line and provide an argument to the instruction FROM
# FROM<argument>

# Some common instructions that you might need to include with a corresponding arugment pattern are shown below
FROM ubuntu:22.04


RUN apt-get update
RUN apt-get install -y python3-pip
COPY ./requirements.txt /requirements.txt
RUN pip3 install -r /requirements.txt
WORKDIR /webApp/

EXPOSE 80

CMD ["python3", "auth.py"]



