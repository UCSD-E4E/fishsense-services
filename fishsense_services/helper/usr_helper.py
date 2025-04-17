import os
from fishsense_database.create_db import database 
from datetime import datetime, timezone, timedelta

import jwt

def get_user_by_email(query_params):
    try:
        with database as db:
            user = db.exec_script("fishsense-database/fishsense_database/scripts/get_scripts/get_users_scripts/get_user.sql", query_params)
            
            return user
    
    except Exception as e:
        print("Error fetching user by email:", e)
        return None
    
def update_last_login(query_params):
    
    try:
        time = unix_time_calc(datetime.now(timezone.utc))
        query_params['last_login'] = time
        
        with database as db:
            user = db.exec_script("fishsense-database/fishsense_database/scripts/update_scripts/update_user_scripts/update_last_login.sql", query_params)
            
            return user
        
    except Exception as e:
        print("Error updating last login:", e)
        return None
    
def unix_time_calc(date : datetime):
    
    unix_time = int(date.timestamp())
    return unix_time

def create_user(query_params):
    
    try:
        print(query_params)
        time = unix_time_calc(datetime.now(timezone.utc))
        query_params['last_login_utc'] = time
        query_params['created_utc'] = time
        
        print("test")
        print(query_params["DOB"])

        query_params["DOB"] = unix_time_calc(query_params["DOB"])
        print("the2nd")
        print(query_params["DOB"])
        
        with database as db:
            res = db.exec_script("fishsense-database/fishsense_database/scripts/insert_scripts/create_user.sql", query_params)
            
            return res
        
    except Exception as e:
        print("Error creating user:", e, flush=True)
        return None
    
def generate_jwt(user_id, email): # TODO add refresh token flow?
    
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(int(os.getenv("JWT_EXP_DELTA_SECONDS"))),
        "iat": datetime.now(timezone.utc)
    }
    
    print("JWT_SECRET:", os.getenv("JWT_SECRET"))
    print("JWT_ALGORITHM:", os.getenv("JWT_ALGORITHM"))

    token = jwt.encode(payload, os.getenv("JWT_SECRET"), algorithm=os.getenv("JWT_ALGORITHM"))
    return token

def validate_jwt(token: str): #TODO needs testing (maybe by setting exp rlly low)
    try:
        payload = jwt.decode(token, os.getenv("JWT_SECRET"), algorithm=os.getenv("JWT_ALGORITHM"))
        return {
            "valid": True,
            "payload": payload
        }
    except jwt.ExpiredSignatureError:
        return {
            "valid": False,
            "error": "Token has expired"
        }
    except jwt.InvalidTokenError:
        return {
            "valid": False,
            "error": "Invalid token"
        }