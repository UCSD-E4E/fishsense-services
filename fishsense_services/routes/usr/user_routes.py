from fishsense_database.database import Database
from create_db import database

import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

usr_router = APIRouter()

@usr_router.post("/create", status_code=200)
async def create_usr_route(request: Request):
    
    data = await request.json()
    
    try:
        
        success = False
        with database as db:
            time_diff = int((datetime.datetime.now(datetime.timezone.utc) - db.time).total_seconds())

            data["created_utc"] = time_diff
            data["last_login_utc"] = time_diff # TODO fix time stamp 
            success = db.exec_script("fishsense-database/scripts/insert_scripts/insert_user.sql", data) 

        if not success:
            raise ValueError("User with this username or email already exists.")

        return JSONResponse(
            status_code=200,
            content={"message": "User created successfully."}
        )
    
    except ValueError as e:
        print(f"ValueError caught in FastAPI: {e}")  # Log the error for debugging
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )
  
        
   
    
# @usr_router.get("/user={username}", status_code=200)
# async def get_usr_route(username: str):

#     try:
#         if not username:
#             raise ValueError("Must provide a username.")
#         user = get_user(username)
        
#         if not user:
#             raise ValueError("User does not exist.")
        
#         return JSONResponse(
#             status_code=200,
#             content={"user details": jsonable_encoder(user)}
#         )
    
#     except ValueError as e:
        
#         return JSONResponse(
#             status_code=400,
#             content={"detail": str(e)}  # Send the error message as string
#         )
    
# @usr_router.get("/", status_code=200)
# async def all_usrs_route():

#     try:
#         users = get_all_users()
        
#         return JSONResponse(
#             status_code=200,
#             content={"users": jsonable_encoder(users)}
#         )
    
#     except ValueError as e:
        
#         return JSONResponse(
#             status_code=400,
#             content={"detail": str(e)}  # Send the error message as string
#         )

# @usr_router.put("/update", status_code=200)
# async def update_usr_route(request: Request):
    
#     data = await request.json()
#     username = data.get("username")
#     new_username = data.get("new_username")
#     new_email = data.get("new_email")
    
#     try:
#         success = update_user(username, new_username, new_email)
        
#         if not success:
#             raise ValueError("User does not exist.")
        
#         return JSONResponse(
#             status_code=200,
#             content={"message": "User updated successfully."}
#         )
    
#     except ValueError as e:
#         return JSONResponse(
#             status_code=400,
#             content={"detail": str(e)}  
#         )

        
# @usr_router.delete("/delete", status_code=200)
# async def delete_usr_route(request: Request):
        
#         data = await request.json()
#         username = data["username"]
        
#         try:
            
#             if not username:
#                 raise ValueError("Must provide a username.")
            
#             success = delete_user(username)
            
#             if not success:
#                 raise ValueError("User does not exist.")
            
#             return JSONResponse(
#                 status_code=200,
#                 content={"message": "User deleted successfully."}
#             )
        
#         except ValueError as e:
            
#             return JSONResponse(
#                 status_code=400,
#                 content={"detail": str(e)}  
#             )
