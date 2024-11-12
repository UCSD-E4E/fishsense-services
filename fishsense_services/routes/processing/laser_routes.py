from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from fishsense_database.laser import get_laser_cal, upload_laser_cal

laser_router = APIRouter()

@laser_router.post("/upload", status_code=200)
async def upload_laser_route(
    laser: UploadFile = File(...),
    results: UploadFile = Form(None),
    dataset_name: str = Form(...),
    user_id: int = Form(...),
    slate_scan: UploadFile = Form(...),
    square_size: float = Form(...),
    rows: int = Form(...),
    cols: int = Form(...)
    
):
    try:
        
        if not user_id or not laser  or not dataset_name or not slate_scan or not square_size or not rows or not cols:
            raise ValueError("Missing required fields") 
        
        laser_content = await laser.read() #TODO convert to ORF
        results_content = await results.read() #TODO convert to csv
        slate_content = await slate_scan.read() #TODO convert to pdf
        
        if results is None:
            #fishsense core calibrate function
            results = None #temp
            #throw related errors
  
        
        # Save the image to the database
        upload_laser_cal(
            laser=laser_content,
            results=results_content,
            dataset_name=dataset_name,
            user_id=user_id,
            slate_scan=slate_content,
            square_size=square_size,
            rows=rows,
            cols=cols
        )
        
        return {"message": "Image uploaded successfully"}
        
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
       