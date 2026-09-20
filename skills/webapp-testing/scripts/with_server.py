#!/usr/bin/env python3
"""
Start one or more servers, wait for them to be ready, run a command, then clean up.

Usage:
    # Single server
    python scripts/with_server.py --server "npm run dev" --port 5173 -- python automation.py
    python scripts/with_server.py --server "npm start" --port 3000 -- python test.py

    # Multiple servers
    python scripts/with_server.py \
      --server "cd backend && python server.py" --port 3000 \
      --server "cd frontend && npm run dev" --port 5173 \
      -- python test.py
"""

import subprocess
import socket
import time
import sys
import argparse
import os
import signal

def port_open(port):
    try:
        with socket.create_connection(('localhost', port), timeout=0.2):
            return True
    except OSError:
        return False


def is_server_ready(port, timeout=30, process=None):
    """Wait for our server, rejecting an early launcher failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if port_open(port):
            return True
        time.sleep(0.05)
    return False


def stop_process(process):
    """Kill the whole session's process group, even if its shell has exited."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def process_options():
    return ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
            else {'start_new_session': True})


def main():
    parser = argparse.ArgumentParser(description='Run command with one or more servers')
    parser.add_argument('--server', action='append', dest='servers', required=True, help='Server command (can be repeated)')
    parser.add_argument('--port', action='append', dest='ports', type=int, required=True, help='Port for each server (must match --server count)')
    parser.add_argument('--timeout', type=int, default=30, help='Timeout in seconds per server (default: 30)')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Command to run after server(s) ready')

    args = parser.parse_args()

    # Remove the '--' separator if present
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]

    if not args.command:
        print("Error: No command specified to run")
        sys.exit(1)

    # Parse server configurations
    if len(args.servers) != len(args.ports):
        print("Error: Number of --server and --port arguments must match")
        sys.exit(1)

    servers = []
    for cmd, port in zip(args.servers, args.ports):
        servers.append({'cmd': cmd, 'port': port})

    if args.timeout <= 0 or len(set(args.ports)) != len(args.ports):
        parser.error("timeout must be positive and ports must be distinct")
    if any(not 1 <= port <= 65535 for port in args.ports):
        parser.error("ports must be between 1 and 65535")
    if any(port_open(port) for port in args.ports):
        parser.error("a requested port is already in use")

    server_processes = []
    command_process = None
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    old_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}

    try:
        # Start all servers
        for i, server in enumerate(servers):
            print(f"Starting server {i+1}/{len(servers)}: {server['cmd']}")

            # Use shell=True to support commands with cd and &&
            process = subprocess.Popen(
                server['cmd'],
                shell=True,
                # Inherit output: no unread pipe can block a noisy server.
                **process_options()
            )
            server_processes.append(process)

            # Wait for this server to be ready
            print(f"Waiting for server on port {server['port']}...")
            if not is_server_ready(server['port'], timeout=args.timeout, process=process):
                raise RuntimeError(f"Server failed to start on port {server['port']} within {args.timeout}s")

            print(f"Server ready on port {server['port']}")

        print(f"\nAll {len(servers)} server(s) ready")

        # Run the command
        print(f"Running: {' '.join(args.command)}\n")
        command_process = subprocess.Popen(args.command, **process_options())
        sys.exit(command_process.wait())

    finally:
        # Ignore repeat interrupts while cleaning up; restore handlers afterwards.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if command_process is not None:
                stop_process(command_process)
            for process in reversed(server_processes):
                stop_process(process)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        print("All server process groups stopped")


if __name__ == '__main__':
    main()