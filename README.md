git clone with --recurse-submodules
remeber to git submodule update!!

whenever you edit fishsense-database
git submodule update --remote --recursive --force

cd into fishsense-database in run
docker exec -it postgres psql -d fishsense_db -U placeholder_superuser
to test database