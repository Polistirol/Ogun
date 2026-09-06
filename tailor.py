#!/usr/bin/env python3
"""
tailor.py
---------
Backward-compatible thin wrapper. Logic lives in author/;
this file keeps the historical CLI:

    python tailor.py --job job_ad.txt --out output/acme_backend/
    python tailor.py --job-text "..." --out output/acme_backend/
    python tailor.py --provider lmstudio --job job_ad.txt --out output/acme_backend/
    python tailor.py --max-iterations 3 --job job_ad.txt --out output/acme_backend/

Output is always a DRAFT to review: no automatic send.
"""

from author.loop import main

if __name__ == "__main__":
    main()
