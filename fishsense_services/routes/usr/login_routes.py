
from datetime import timedelta, datetime, timezone
from os import abort
import os
from typing import Dict
import jwt

from pydantic import BaseModel

from fastapi import APIRouter, Request
from google.auth.transport import requests
from google.oauth2 import id_token

# This is public information.
CLIENT_ID = "585544089882-2e8mni8kmbs39kekip1k6d09q5gjmqvv.apps.googleusercontent.com" #might need to be changed

login_router = APIRouter()

# possible routes:  need to check if there is account associated with it
# Look at login flow for UCSD E4E FishSense-webapp readme

JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM')
 
class Token(BaseModel):
    access_token: str
    token_type: str


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt



@login_router.post("/login", response_model=Token)
def login():
    
    #havent tested below code
    
    #TODO GET INFO FROM DATABASE, namely oauth token if not exists, return 401 and redirect to create_user flow
    
    
    # scripts in fishsense-database/fishsense_database/scripts/
    # ready scripts: create_user.sql, get_user_by_email.sql (did some changes that i havent tested here)
    #TODO Update last login time (do it in fishsense_database not here)
    #TODO validate outh token (verify token method below)
    
    #if token is valid, create a jwt token and return it
    token = create_access_token(data={"sub": "test"}, expires_delta=30) #replace data with user id and name?
    
    return Token(access_token=token, token_type="bearer")
    
    



@login_router.post("/api/login")
def verify_token():
    try:
        token: Dict[str, str] = Request.json
        idinfo = id_token.verify_oauth2_token(
            token["credential"], requests.Request(), CLIENT_ID
        )

        userid: str = idinfo["sub"]
        return userid
    except ValueError as e:
        return abort(401, description=e.args)
