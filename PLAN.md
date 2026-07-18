FishSense now has multiple tenants.  We need to adjust our policies.  FishSense also has multiple kinds of devices.

This is the current service running and it handles the fishsense lite
https://github.com/UCSD-E4E/fishsense-lite/

https://github.com/UCSD-E4E/fishsense-mobile/ is the source code for one of the devices

Known current devices
FishSense Lite - a system that uses the relationship between a fixed laser and a camera to be able to measure distane to the camera.  then uses camera calibration to estimate length of fish in the water
FishSense Mobile - an iPhone Pro which uses lidar to measure fish above water.

Future Iterations
FishSense Mobile (Multilense) - Uses multiple lenses to be able to estimate fish length on mobile devices without lidar above water
FishSense Mono - built upon FishSense Lite.  Uses ML to estimate monocular depth
FishSense Scout - an ROV/ camera trap system that is built on top of the FishSense Mono technology

This service needs to be able to recieve/process/store data from these. it also needs to support login for fishsense mobile.

We have an authentik system provided by
https://github.com/KastnerRG/krg-infra

Here are some more interesting links
http://kastner.ucsd.edu/wp-content/uploads/2025/08/admin/oceans2025-fishsenseMobile.pdf
https://e4e.ucsd.edu/fishsense/
https://e4e.ucsd.edu/fishsense-lite
https://e4e.ucsd.edu/fishsense-mobile
https://e4e.ucsd.edu/fishsense-scout