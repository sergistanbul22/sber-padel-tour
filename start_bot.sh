#!/bin/bash
cd /root/.openclaw/workspace
./bot_venv/bin/python3 tournament_bot.py > /tmp/tournament_bot.log 2>&1 &
echo "Started with PID $!"
