from fastapi import APIRouter, UploadFile, File, Form
from typing import List

from fishsense_services.helper.usr_helper import validate_jwt



laser_router = APIRouter()

class LaserCalRequest:
    """
    Request model for laser calibration.
    """
    jwt: str
    laser_orfs: list

@laser_router.post("/process", status_code=200)

async def process_laser_data(
    jwt: str = Form(...),
    laser_orfs: List[UploadFile] = File(...)
):
    valid_jwt = validate_jwt(jwt)
    if not valid_jwt["valid"]:
        return {"error": "Invalid JWT token."}
    
    #grab laser_orfs (ORF image files) andm filter through and make sure they are all ORF files
    for file in laser_orfs:
        if not file.filename.lower().endswith(".orf"):
            return {"error": f"Invalid file type: {file.filename}. All files must be .orf"}
        
    # Process the laser ORF files
    

    
    # Placeholder for processing logic
    return {"message": "Laser data processed successfully."}