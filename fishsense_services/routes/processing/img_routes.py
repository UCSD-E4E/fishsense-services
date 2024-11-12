from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from pydantic import BaseModel
from fishsense_database.image import upload_img, get_all_user_imgs, get_img
from fishsense_database.laser import get_laser_cal
from fishsense_database.lens import get_lens_cal
import os

img_router = APIRouter()

@img_router.post("/upload", status_code=200)
async def upload_img_route(
    username: str = Form(...),
    img_name: str = Form(...),
    img: UploadFile = File(...),
    length: float = Form(None),
    units: str = Form(None),
    dataset_name: str = Form(...),
    # lens_calibration_id: int = Form(None),
    # laser_calibration_id: int = Form(None),
    version_num: int = Form(None)
):
    try:
        
        if not username or not img_name or not img or not dataset_name:
            raise ValueError("Missing required fields") 
        
        image_content = await img.read() #TODO convert to ORF
        
        if length is None:
            #fishsense core get length function
            length = 0 #temp length'
            units = "cm" #temp units
            #throw related errors
            
        laser_calibration_id = get_laser_cal(username, dataset_name)[0]
        if laser_calibration_id is None:
            laser_calibration_id = None
        lens_calibration_id = get_lens_cal(username, dataset_name)[0]
        if lens_calibration_id is None:
            lens_calibration_id = None
            
        
        
        
        # Save the image to the database
        upload_img(
            name=img_name,
            image=image_content,
            user_id=username,
            length=length,
            units=units,
            dataset_name=dataset_name,
            lens_calibration_id=lens_calibration_id,
            laser_calibration_id=laser_calibration_id,
            version_num=version_num
        )
        
        return {"message": "Image uploaded successfully"}
        
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
       
    
@img_router.get("/{username}/{img_id}", status_code=200)
async def get_img(username: str, img_id: str):
    
    try:
        if not username or not img_id:
            raise ValueError("Missing required fields")
        
        img = get_img(username, img_id)
        
        if not img:
            raise ValueError("Image does not exist")
        return img
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=e.args)
    
@img_router.get("/gallery/{username}")
async def get_all_imgs(username: str, email: str | None = None):
    
    try:
        if not username:
            raise ValueError("Missing required fields") 
        # TODO convert form image path to image content
        imgs = get_all_user_imgs(username, email)
        
        if not imgs:
            raise ValueError("User does not exist")
        
        return imgs
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail="User does not exist")

@img_router.delete("/{username}/{img_name}", status_code=200)
async def delete_img(username: str, img_name: str):
    
    try:
        delete_img(username, img_name)
        return
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=e.args)
    
        
    