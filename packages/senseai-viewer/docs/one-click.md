# One click from S3 to a Panther report

1. Open the tunnel from your laptop: `ssh -L 8000:localhost:8000 ubuntu@<ip>` (the viewer only listens on the box itself).
2. Open <http://localhost:8000/run>. If Setup says "needs attention", press **Install / repair**. If S3 is refused, copy the policy shown into the instance role.
3. Under **Recordings**, tick the night drives you want and press **Fetch & prepare**. They are downloaded, unpacked, arranged and checked, and each turns **ready**.
4. Under **Run**, tick what to train on and what to test on (never the same drive), keep the contestants, and press **Run POC**.
5. Watch **Progress**; you can close the laptop. When the run is done, **Results** has the head-to-head, **Watch side by side** and **Share**.

First time on a new box only (one command, then never again):
`git clone https://github.com/senseai-institute/viewer ~/senseai/viewer && bash ~/senseai/viewer/packages/senseai-viewer/deploy/setup_ec2.sh --service`
