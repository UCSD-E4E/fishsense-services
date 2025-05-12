import os
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import List
import tempfile
import shutil
from pathlib import Path
import json

from fastapi.responses import JSONResponse

from fishsense_services.helper.fish_helper import fish
from fishsense_services.helper.usr_helper import validate_jwt

fish_router = APIRouter()


@fish_router.post("/process", status_code=200)
async def process_fish_data(
    email: str = Form(...),
    jwt: str = Form(...),
    fish_orfs: List[UploadFile] = File(...)
):
    try:
        valid_jwt = validate_jwt(jwt, email)

        if not valid_jwt["valid"]:
            raise HTTPException(status_code=401, detail="Invalid JWT token.") #TODO FIX ERROR HANDLING
        
        #grab laser_orfs (ORF image files) andm filter through and make sure they are all ORF files
        res = {}
        
        for file in fish_orfs:
            if not file.filename.lower().endswith(".orf"):
                res[file.filename] = f"Invalid file type: {file.filename}. All files must be .orf"
                
            else:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".orf") as temp_file:
                    shutil.copyfileobj(file.file, temp_file)
                    temp_path = Path(temp_file.name)

                try:
                    fish_result = fish(temp_path)

                    print("FINISHED PROCESSING FISH", flush=True)

                    res[file.filename] = {
                        "result": fish_result
                    }
                    
                except Exception as e:
                    res[file.filename] = {
                        "error": str(e)
                    }
            
        return JSONResponse(
            status_code=200,
            content={
                "message": "Fish data processed successfully.",
                "results": res
            }
        )
        
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail}
        )