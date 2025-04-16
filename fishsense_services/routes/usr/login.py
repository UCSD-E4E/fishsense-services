from datetime import date
import json
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.auth.transport import requests
from google.oauth2 import id_token
import os

from fishsense_services.helper.usr_helper import get_user_by_email, update_last_login, create_user

login_router = APIRouter()

# Google client ID
CLIENT_ID = "931946598531-mbukvvb7g21kdifbf67g64igk036ect4.apps.googleusercontent.com"
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")  # Load from env vars securely

class TokenRequest(BaseModel):
    credential: str
    
class createUserRequest(BaseModel):
    username: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    DOB: Optional[date] = None
    oauth_id: str
    credential: str
    organization_name: Optional[str] = None #TODO need to add to form later
    
@login_router.post("/api/account")
async def verify_token(request : Request):
    try:
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
        print("Credential:", token.credential)
        print(type(token))

        idinfo = id_token.verify_oauth2_token(
            token.credential, requests.Request(), CLIENT_ID
        )
        
        print("Verified Token Info:", idinfo)
        
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
        
        print("updating last login")
        update_last_login(query_params)
        print("returning values")
        print(res)
        
        return JSONResponse(
            content={
                "username": res[0][0][1],
                "email": res[0][0][2],
                "creation_date": res[0][0][3],
                "last login date": res[0][0][4],
                "first name": res[0][0][5],
                "last name": res[0][0][6],
                "DOB": res[0][0][7],
                "google_photo": user_picture
            },
          
        )

    except HTTPException as e:
        if e.status_code == 404:
            print("Caught 404: Not Found")
            raise HTTPException(status_code=404, detail="User not found")
        else:
            raise HTTPException(status_code=401, detail="Unauthorized")
    
@login_router.post("/create-user")
async def create_account(userRequest: createUserRequest):
    
    print(type(userRequest))
    
    try:
        print("Creating query")
        
        query_params = userRequest.model_dump()
        del query_params['credential']
        
        print("calling exec script")
        
        res = create_user(query_params)
        
        if not res:
            print("unable to create user")
            raise HTTPException(status_code=409, detail="Conflict: Username or Email already exists")#TODO make less repetitive

        return JSONResponse(
            content={
                "status": "Success"
            },
            
        )
    except HTTPException as e:
        if e.status_code == 409:
            print("Caught 404: Not Found")
            raise HTTPException(status_code=409, detail="Conflict: Username or Email already exists")
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
