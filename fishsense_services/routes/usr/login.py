
from os import abort
from typing import Dict

from fastapi import APIRouter, Request
from google.auth.transport import requests
from google.oauth2 import id_token
from starlette.responses import RedirectResponse

# This is public information.
CLIENT_ID = "585544089882-2e8mni8kmbs39kekip1k6d09q5gjmqvv.apps.googleusercontent.com"

login_router = APIRouter()

# Starting Google OAuth
@login_router.get("/api/login")
def login_with_google():
    google_auth_url = (
        "https://accounts.google.com/o/oauth2/auth?"
        "response_type=code&"
        "client_id={}&"
        "redirect_uri=http://localhost:8000/login/callback&"
        "scope=email profile&"
        "access_type=online"
    ).format(CLIENT_ID)
    return RedirectResponse(google_auth_url)

# Callback route to handle the Google OAuth response
@login_router.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code not provided")
    try:
        idinfo = id_token.verify_oauth2_token(
            token=code, request=requests.Request(), audience=CLIENT_ID
        )
        userid = idinfo["sub"]
        email = idinfo["email"]
        return {"userid": userid, "email": email}

    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid token")



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
