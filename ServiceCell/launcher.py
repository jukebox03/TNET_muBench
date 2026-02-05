import sys
import os
from dpumesh.server import main

if __name__ == "__main__":
    # Ensure the current directory is in the path
    sys.path.append(os.getcwd())
    
    # Simulate CLI arguments for dpumesh-server
    # Syntax: dpumesh-server <module:app>
    sys.argv = ["dpumesh-server", "CellController_mp_dpu:app"]
    
    print("[LAUNCHER] Starting dpumesh-server via module import...", flush=True)
    main()
