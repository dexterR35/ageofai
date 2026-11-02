# Observer and reliability improvements

The observer separates navigation, civilization resources and live connection status. Civilization cards expand the selected society while keeping the other societies compact. Map controls provide centering, zoom and a desktop panel toggle. On smaller screens, Kingdoms, Thoughts and Journal buttons open the corresponding panel. Tabs support arrow keys, Home and End, and controls have visible keyboard focus.

Connection failures show the last received state and a retry action. Retries back off to 30 seconds; a connection that stops sending frames is replaced. Each connection receives current resource nodes, roads and fog alongside recent history. If a slow viewer loses a queued delta, the server asks it to resynchronize. Reconnecting preserves the camera and rebuilds diplomacy messages. Idle disconnected sockets release their server queues.

Stats, Evolution, Diplomacy and Chronicle show a retry message after failed requests. Timelapse requests release their button after a failure, and returning to live restores the map state. Unchanged resource and civilization markup is reused. Map animation stops while another view or a hidden browser tab is active; data refreshes avoid overlapping requests.

In civilization mode, manufacturing scales production and input costs to available output storage, including partial batches. Newly available adults fill vacant offices without waiting for the weekly review. Government and economy effects are calculated once per institution tick, and research trials share a single deposit scan while retaining independent trial randomness.

## Focused verification

These checks use temporary databases, fake model clients and deterministic clock inputs. They do not launch the live game or model training:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_v4.py tests/test_development.py tests/test_pacing.py tests/test_observer.py tests/test_live_recovery.py
node --test tests/observer_client.test.mjs
node --check web/app.js
node --check web/map.js
```

Result on 1 November 2026: 55 Python tests and 7 JavaScript tests passed. The disposable observer preview was stopped at the user's request. Full visual verification and a sustained autonomous simulation remain pending; no live game is left running by this improvement pass.
