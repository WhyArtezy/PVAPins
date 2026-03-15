import requests
import time
import threading
import os

API_KEY = "APIKEY_KAMU"
COUNTRY = "Mexico"

TARGET = 5

APPS = [
    "Whatsapp57",
    "Whatsapp33",
    "Whatsapp56",
    "Whatsapp9",
    "Whatsapp1"
]

numbers = []
otp = {}

# limiter request
REQUEST_DELAY = 1.2   # ~50 req/min aman

lock = threading.Lock()


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def dashboard():

    while True:

        clear()

        print("===== PVAPins OTP Dashboard =====\n")

        print("Target nomor :", TARGET)
        print("Nomor aktif  :", len(numbers))
        print("OTP masuk    :", len(otp))

        print("\nList nomor\n")

        for n in numbers:

            if n in otp:
                print(n, " -> OTP :", otp[n])
            else:
                print(n, " -> waiting")

        time.sleep(2)


def get_number():

    while len(numbers) < TARGET:

        for app in APPS:

            url = f"https://api.pvapins.com/user/api/get_number.php?customer={API_KEY}&country={COUNTRY}&app={app}"

            try:

                r = requests.get(url,timeout=10)
                res = r.text.strip()

                if "No free" in res:
                    continue

                with lock:

                    if res not in numbers:

                        numbers.append(res)
                        print("Nomor baru :",res,"|",app)

            except:
                pass

            time.sleep(REQUEST_DELAY)


def monitor_sms():

    while True:

        for number in numbers:

            if number in otp:
                continue

            for app in APPS:

                try:

                    url = f"https://api.pvapins.com/user/api/get_sms.php?customer={API_KEY}&number={number}&country={COUNTRY}&app={app}"

                    r = requests.get(url,timeout=10)
                    res = r.text.strip()

                    if "code" in res.lower() or res.isdigit():

                        otp[number] = res
                        print("OTP masuk :",number,res)

                except:
                    pass

                time.sleep(REQUEST_DELAY)

        time.sleep(3)


# start dashboard
threading.Thread(target=dashboard,daemon=True).start()

# start order nomor
threading.Thread(target=get_number).start()

# start monitor otp
threading.Thread(target=monitor_sms).start()
