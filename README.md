# Blueprints_DB
Database for Blueprints for Pangaea

Translating old data from google sheets inventory sheet and internal/external dashboard that are connected into proper postgreSQL. Work in progress.

Here is schema (BCNF) if interested:

<img width="1293" height="491" alt="Screenshot 2026-09-06 at 7 46 40 PM" src="https://github.com/user-attachments/assets/19e97221-62c0-4a31-8599-53a0bacf3bf3" />

I want to make this simpler and cleaner as well, but I need to analyze the incoming app data as well to see what users input and what columns may be used or not used

**UPDATE: 9/6/26, Designed + implemented schema, database is up and running.** 

TODO:
1. Check it works well end to end when users edit data on dashboard or upload new data during inventorying
2. See if we can pull data from the inventorying app to get timestamps for date inventoried. Currently, there are no dates
inventoried, so all of them initialized to today. Not the biggest deal as expiration date matters more
3. Make more friendly UI dashboard for easy viewing. Make sure no bugs, fully push on vercel
