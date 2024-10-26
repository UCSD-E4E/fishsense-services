
from os import abort
from typing import Dict

from fastapi import APIRouter, Request
from google.auth.transport import requests
from google.oauth2 import id_token

# This is public information.
CLIENT_ID = "585544089882-2e8mni8kmbs39kekip1k6d09q5gjmqvv.apps.googleusercontent.com"

login_router = APIRouter()


# possible routes: reset passwrod, need to check if there is account associated with it

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
