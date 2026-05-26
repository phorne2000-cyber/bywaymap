# HotSausage Byways 6.4 QCT UI Fix

Fixes:
- Ready QCT maps display in Planner as selectable base maps.
- `/planner?map_id=<id>` auto-loads the selected QCT.
- `/maps` has Delete buttons for bad QCT uploads.
- Adds `DELETE /maps/{map_id}`.

Build:
```powershell
python -m py_compile app\main.py
docker build --no-cache -t criticalmass303/bywaymap:6.4 .
docker push criticalmass303/bywaymap:6.4
```

Deploy:
```bash
kubectl set image deployment/bywaymap bywaymap=criticalmass303/bywaymap:6.4 -n office
kubectl rollout status deployment/bywaymap -n office
```
