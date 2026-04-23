from dotenv import load_dotenv
import os

load_dotenv()

REINFOLIB_API_KEY = os.getenv("REINFOLIB_API_KEY", "")
