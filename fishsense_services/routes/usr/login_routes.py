from datetime import timedelta, datetime, timezone
from os import abort
import os
from typing import Dict
import jwt
from authlib.integrations.starlette_client import OAuth


from pydantic import BaseModel

from google.auth.transport import requests
from google.oauth2 import id_token
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fishsense_database.database import HTTP_CODES, Database

# This is public information.
import os

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM')
print(f"JWT_SECRET: {JWT_SECRET}, JWT_ALGORITHM: {JWT_ALGORITHM}")

login_router = APIRouter()

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    authorize_url="https://accounts.google.com/o/oauth2/auth",
    authorize_params=None,
    access_token_url="https://oauth2.googleapis.com/token",
    access_token_params=None,
    client_kwargs={"scope": "openid email profile"},
)

# possible routes:  need to check if there is account associated with it
# Look at login flow for UCSD E4E FishSense-webapp readme
 
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



@login_router.post("/", response_model=Token)
async def login(request: Request):
    
    #havent tested below code
    
    #TODO GET INFO FROM DATABASE, namely oauth token if not exists, return 401 and redirect to create_user flow
    body = await request.json() 
    # Do we need key that is sent from frontend?
    oauth_token = body.get("credential")

    if not oauth_token:
        return JSONResponse(status_code=400, content={"error": "Missing OAuth token"})
    
    idinfo = id_token.verify_oauth2_token(oauth_token, requests.Request(), GOOGLE_CLIENT_ID)
    # Extracting the user's email if needed, could also extract other stuffs from user info
    user_email = idinfo.get("email") 

    # Check if the user exists in the database
    user_exists = False
    with Database() as db:
        user_query = db.exec_script(
            "fishsense-database/fishsense_database/scripts/get_scripts/get_users_scripts/get_user.sql",  
            HTTP_CODES.GET.value, 
            (user_email,)
        )
        user_exists = bool(user_query)

    # If user doesn't exist, create user flow (when user_exists = False)
    # no need anymore, frontend handles it
    if not user_exists:
            # NO Longer needed
            # Redirect to create_user flow
            # create_user_data = {
            #     "username": user_email.split("@")[0],
            #     "email": user_email,
            #     # Do we need default DOB?
            #     "DOB": "2025-01-01",
            #     "first_name": idinfo.get("given_name", "DefaultFirstName"), 
            #     "last_name": idinfo.get("family_name", "DefaultLastName"), 
            #     "organization_name": None,
            # }

            ## no just redirect to 401 error
            return JSONResponse(
                status_code=401,
                content={"error": "User does not exist. Redirecting to the sign up page."}
            )
    
            # NO need anymore
            # with Database() as db:
            #     time_diff = db.calc_time_diff(datetime.datetime.now(datetime.timezone.utc))
            #     create_user_data["created_utc"] = time_diff
            #     create_user_data["last_login_utc"] = time_diff
            #     create_user_data["DOB"] = db.calc_time_diff(
            #         datetime.datetime.strptime(create_user_data["DOB"], "%Y-%m-%d")
            #     )

            #     err = db.exec_script("fishsense-database/fishsense_database/scripts/insert_scripts/create_user.sql",HTTP_CODES.POST, create_user_data,)
            #     if err:
            #         raise ValueError(f"Failed to create user: {err}")

    
    # scripts in fishsense-database/fishsense_database/scripts/
    # ready scripts: create_user.sql, get_user_by_email.sql (did some changes that i havent tested here)
    #TODO Update last login time (do it in fishsense_database not here)
    with Database() as db:
        print(f"Updating last login time for user for debugging purpose: {user_email}")
        err = db.exec_script(
            "fishsense-database/fishsense_database/scripts/update_scripts/update_user_scripts/update_last_login.sql",
            # "fishsense-database/fishsense_database/scripts/update_scripts/update_last_login.sql",
            HTTP_CODES.PUT,
            (user_email,)
        )

    
    if err:
        raise ValueError(f"Error in updating last login time: {err}")
    

    #TODO validate outh token (verify token method below)
    token = create_access_token(data={"sub": user_email}, expires_delta=timedelta(minutes=30)) 

    # jwt token updating the database
    with Database() as db:
        print(f"JWT token for user for debugging purpose: {user_email}")
        jwt_token = db.exec_script(
            "fishsense-database/fishsense_database/scripts/update_scripts/update_user_scripts/update_jwt_token.sql",
            HTTP_CODES.PUT,
            (token, user_email)
        )
        if jwt_token is None:
            raise ValueError("Failed to update JWT token to the database")
    
    return Token(access_token=token, token_type="bearer")
    # #if token is valid, create a jwt token and return it
    # token = create_access_token(data={"sub": "test"}, expires_delta=30) #replace data with user id and name?
    
    # return Token(access_token=token, token_type="bearer")
    
    



@login_router.post("/api/login")
def verify_token():
    try:
        token: Dict[str, str] = Request.json
        idinfo = id_token.verify_oauth2_token(
            token["credential"], requests.Request(), GOOGLE_CLIENT_ID
        )

        userid: str = idinfo["sub"]
        return userid
    except ValueError as e:
        return abort(401, description=e.args)


