import json
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.auth.transport import requests
from google.oauth2 import id_token
import os

from fishsense_services.helper.usr_helper import get_user_by_email, update_last_login

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
    credential: str

@login_router.post("/api/account")
async def login_with_google(token: TokenRequest):
    try:
        print("Credential:", token.credential)

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
            return JSONResponse(
                content={
                    "Status": "User not found"
                }
            )
        print("updating last login")
        update_last_login(query_params)
        print("returning values")
        
        return JSONResponse(
            content={
                "username": res[1],
                "email": res[2],
                "creation_date": res[3],
                "last login date": res[4],
                "first name": res[5],
                "last name": res[6],
                "DOB": res[7]
            },
          
        )

    except Exception as e:
        print("Login failed:", str(e))
        raise HTTPException(status_code=401, detail="Unauthorized")
    
@login_router.get("/create_user")
def create_user(
    

    
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
