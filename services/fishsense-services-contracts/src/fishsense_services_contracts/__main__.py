import json

from fishsense_services_contracts import json_schema

print(json.dumps(json_schema(), indent=2, sort_keys=True))
