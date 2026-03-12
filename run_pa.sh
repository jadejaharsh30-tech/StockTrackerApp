#!/bin/bash
# Helper script to run daily tasks within the 80-character limit of the PythonAnywhere API
# This avoids "Ensure this field has no more than 80 characters" error.

~/.virtualenvs/myenv/bin/python ~/StockTrackerApp/daily_tasks.py
