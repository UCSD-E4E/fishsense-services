from pyfishsensedev.segmentation.fish import FishSegmentationFishialPyTorch, FishSegmentationFishialOnnx
from pyfishsensedev.points_of_interest.fish import FishPointsOfInterestDetector
from pyfishsensedev.image import ImageRectifier, RawProcessor
from pyfishsensedev.depth_map import LaserDepthMap
from pyfishsensedev.laser.nn_laser_detector import NNLaserDetector
from pyfishsensedev.calibration import LensCalibration, LaserCalibration

from pathlib import Path
import numpy as np
import torch
import matplotlib.pyplot as plt
import io
import base64
from PIL import Image
from io import BytesIO

def fig_to_base64(fig):
    buf = BytesIO()
    fig.savefig(buf, format='png')  
    buf.seek(0)
    img_str = base64.b64encode(buf.read()).decode('utf-8')  
    return img_str

def mask_to_base64(mask):
    pil_image = Image.fromarray(mask.astype(np.uint8) * 255)  
    buf = BytesIO()
    pil_image.save(buf, format='PNG')  
    buf.seek(0)
    img_str = base64.b64encode(buf.read()).decode('utf-8')  
    return img_str

def uint16_2_double(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float64) / 65535

def uint16_2_uint8(img: np.ndarray) -> np.ndarray:
    return (uint16_2_double(img) * 255).astype(np.uint8)


def fish(input_file):
    
    try:
    
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        fish_dict = {}
        
        # TODO lens cal is connected to camera class, perhaps when we upload lens_cal, we upload associated camera then also pass in the camera type with the query params
        lens_calibration_path = Path("fishsense_services/temp/data/lens-calibration.pkg")
        
        # TODO: find the connected deployment in the database (perhaps in the request body then as a param fo rthis function and then find the associated laser cal)
        laser_calibration_path = Path("fishsense_services/temp/data/laser-calibration.pkg")

        lens_calibration = LensCalibration()
        lens_calibration.load(lens_calibration_path)

        laser_calibration = LaserCalibration()
        laser_calibration.load(laser_calibration_path)
        
        raw_processor_hist_eq = RawProcessor()
        raw_processor = RawProcessor(enable_histogram_equalization=False)

        image_rectifier = ImageRectifier(lens_calibration)
        
        img = raw_processor_hist_eq.load_and_process(Path(input_file))
        img_dark = raw_processor.load_and_process(Path(input_file))

        img = image_rectifier.rectify(img)
        img_dark = image_rectifier.rectify(img_dark)

        img8 = uint16_2_uint8(img)
        img_dark8 = uint16_2_uint8(img_dark)
        
        laser_detector = NNLaserDetector(lens_calibration, laser_calibration, device)
        laser_coords = laser_detector.find_laser(img_dark8)
        laser_coords_int = np.round(laser_coords).astype(int)
        
        fig, ax = plt.subplots(figsize=(img8.shape[1] / 100, img8.shape[0] / 100), dpi=100)
        ax.imshow(img8, cmap='gray')
        ax.plot(laser_coords[0], laser_coords[1], 'r.', markersize=10)
        ax.axis('off')
        plt.tight_layout(pad=0)

        fish_dict["laser_img"] = fig_to_base64(fig)
        fish_dict["laser_coords"] = laser_coords.tolist()
        plt.close(fig)
        
        fish_segmentation_inference = FishSegmentationFishialOnnx()

        segmentations = fish_segmentation_inference.inference(img8)

        mask = np.zeros_like(segmentations, dtype=bool)
        mask[segmentations == segmentations[laser_coords_int[1], laser_coords_int[0]]] = True
        
        fish_dict["mask"] = mask_to_base64(mask)
        
        fish_head_tail_detector = FishPointsOfInterestDetector()
        left_coord, right_coord = fish_head_tail_detector.find_points_of_interest(mask)
        
        fig, ax = plt.subplots()
        ax.imshow(img, cmap='gray')
        ax.plot(left_coord[0], left_coord[1], 'g.', markersize=10)  # green
        ax.plot(right_coord[0], right_coord[1], 'b.', markersize=10)  # blue
        ax.axis('off')
            
        depth_map = LaserDepthMap(laser_coords, lens_calibration, laser_calibration)
        image_height, image_width, _ = img.shape
        
        fish_dict["depth_map"] = depth_map.depth_map[0, 0]

        left_coord3d = depth_map.get_camera_space_point(left_coord, image_width, image_height, lens_calibration)
        right_coord3d = depth_map.get_camera_space_point(right_coord, image_width, image_height, lens_calibration)

        length = np.linalg.norm(left_coord3d - right_coord3d)
        
        fish_dict["length"] = length
        
        return fish_dict
    
    except Exception as e:
        raise e