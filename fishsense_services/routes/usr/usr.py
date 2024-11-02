from fishsense_database.user import create_user, get_user, update_user, delete_user, get_all_users
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

usr_router = APIRouter()

@usr_router.post("/create", status_code=200)
async def create_usr(request: Request):
    
    data = await request.json()
    username = data["username"]
    email = data["email"]
    
    try:
        id = create_user(username, email)
        
        if not id:
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
  
        
   
    
@usr_router.get("/user={username}", status_code=200)
async def get_usr(username: str):

    try:
        if not username:
            raise ValueError("Must provide a username.")
        user = get_user(username)
        
        if not user:
            raise ValueError("User does not exist.")
        
        return JSONResponse(
            status_code=200,
            content={"user details": jsonable_encoder(user)}
        )
    
    except ValueError as e:
        
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )
    
@usr_router.get("/", status_code=200)
async def all_usrs():

    try:
        users = get_all_users()
        
        return JSONResponse(
            status_code=200,
            content={"users": jsonable_encoder(users)}
        )
    
    except ValueError as e:
        
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )

