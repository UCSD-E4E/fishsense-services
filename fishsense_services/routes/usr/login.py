from datetime import datetime
import json
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.auth.transport import requests
from google.oauth2 import id_token
import os

from fishsense_services.helper.usr_helper import generate_jwt, get_user_by_email, update_last_login, create_user

login_router = APIRouter()

# Google client ID
CLIENT_ID = "931946598531-u2kdslb2ht5gbhrkb1t9hhre2br71c23.apps.googleusercontent.com"
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")  # Load from env vars securely

class TokenRequest(BaseModel):
    credential: str
    
class createUserRequest(BaseModel):
    username: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    DOB: Optional[datetime] = None #TODO bug fix where you have to fill out DOB
    credential: str
    organization_name: Optional[str] = None
    
# @login_router.options("/api/account")
# async def preflight_handler():
#     print("OPTIONS preflight hit!", flush=True)
#     return JSONResponse(status_code=200, content={"message": "OK"})
    
@login_router.post("/api/account")
async def verify_token(request : Request):
    try:
        # print("CORS", flush = True)

        token: Dict[str, str] = await request.json()
        
        
        idinfo = id_token.verify_oauth2_token(
            token["credential"], requests.Request(), CLIENT_ID
        )

        userid: str = idinfo["sub"]
        return userid
    
    except ValueError as e:
        raise HTTPException(401, description=e.args)

@login_router.post("/")
async def login_with_google(token: TokenRequest):
    try:
        idinfo = id_token.verify_oauth2_token(
            token.credential, requests.Request(), CLIENT_ID
        )
                
        userid: str = idinfo["sub"]
        user_email = idinfo["email"]
        user_name = idinfo.get("name")
        user_picture = idinfo.get("picture")

        print("checking if user exists")
        query_params = {"email" : user_email}
        res = get_user_by_email(query_params)
        print(res)
        
        if not res:
            print("doesn't exist :(")
            raise HTTPException(status_code=404, detail="User not found")#TODO make less repetitive
        
        res = res[0][0]
        res = res.strip('()').split(',')
        print(res)
        
        
        userid: str = idinfo["sub"]
        user_email = idinfo["email"]
        user_name = idinfo.get("name")
        user_picture = idinfo.get("picture")
        
        print("updating last login")
        update_last_login(query_params)
        print("returning values")

        jwt_token = generate_jwt(int(res[0]), user_email)

        return JSONResponse(
            status_code=200,
            content={
                "username": res[1],
                "email": res[2],
                "creation_date": res[3], #TODO returned as unix time and BIG INT fix?
                "last login date": res[4],
                "first name": res[5],
                "last name": res[6],
                "DOB": res[7],
                "google_photo": user_picture,
                "token": jwt_token
            },
          
        )

    except HTTPException as e:
        if e.status_code == 404:
            print("Caught 404: Not Found")
            raise HTTPException(status_code=404, detail="User not found")
        else:
            raise HTTPException(status_code=401, detail="Unauthorized")
    
@login_router.post("/create-user") #TODO not storing oauth-id information in database despite database diagram says differently
async def create_account(userRequest: createUserRequest): #TODO future confirmation or verificatoin email?
    
    print(type(userRequest))
    
    try:
        print("Creating query")
        
        query_params = userRequest.model_dump()
        del query_params['credential']
        
        print("calling exec script")
        
        res = create_user(query_params)
        print(res)
    
        if not res or res[0][0] == -1:
            print("unable to create user")
            raise HTTPException(status_code=409, detail="Conflict: Username or Email already exists")#TODO make less repetitive

        jwt_token = generate_jwt(int(res[0][0]), query_params["email"])
        
        return JSONResponse(
            status_code=200,
            content={
                "status": "Success",
                "jwt": jwt_token
            },
            
        )
    except HTTPException as e: #TODO fix exceptions
        if e.status_code == 409:
            raise HTTPException(status_code=409, detail=str(e))
        else:
            raise HTTPException(status_code=401, detail="Unauthorized")
    
    

    

    
# @login_router.get("/auth/callback")
# async def auth_callback(request: Request):
#     code = request.query_params.get("code")
#     if code:
#         # Exchange the code for an access token using your backend
#         # Use Google API's OAuth2 libraries to exchange code for token
#         # For example, you might use Google's `requests` library to fetch the token
#         # after validating the code.
#         return RedirectResponse(url="/")  # Redirect to the homepage or another page
#     return {"error": "Code missing"}
