#!/usr/bin/env python3
"""Run a command completely hidden (no console window). Usage: python run_hidden.py <command> [args...]"""
import subprocess, sys, os
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # cd to F:\DouyinPipeline
cmd = sys.argv[1:]
if cmd:
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
