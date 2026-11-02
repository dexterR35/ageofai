# Age of AI — stop starting the world at logon (removes the "Age of AI" scheduled task).
Stop-ScheduledTask -TaskName "Age of AI" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Age of AI" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "The 'Age of AI' task is removed. A world that is running keeps running until you close it."
