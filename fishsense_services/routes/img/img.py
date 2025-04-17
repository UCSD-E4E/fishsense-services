from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from fishsense_database.database import Database 
from fishsense_database.image import upload_img, get_all_user_imgs, get_img

img_router = APIRouter()

# @img_router.get("/gallery/{username}")
# async def get_all_imgs(username: str, email: str | None = None):
    
#     try:
#         imgs = get_all_user_imgs(username, email)
#         return imgs
    
#     except ValueError as e:
#         raise HTTPException(status_code=404, detail="User does not exist")

@img_router.post("/upload", status_code=200)
async def upload_img():
    
    try:
        data = await Request.json()
        username = data["username"]
        img_name = data["img_name"]
        file_type = data["file_type"]
        img = data["img"]
        # TODO add other parameters
        
        upload_img(username, img_name, file_type, img)
        
        return 
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail="User does not exist")
    
    
@img_router.get("/{username}/{img_id}", status_code=200)
async def get_img(username: str, img_id: str):
    
    try:
        img = get_img(username, img_id)
        return img
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=e.args)
    
    
@img_router.delete("/{username}/{img_name}", status_code=200)
async def delete_img(username: str, img_name: str):
    
    try:
        delete_img(username, img_name)
        return
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=e.args)
    
        
    