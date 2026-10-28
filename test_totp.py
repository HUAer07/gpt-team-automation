import pyotp
import time

def verify_totp(secret: str):
    clean_secret = secret.replace(" ", "").strip()
    try:
        totp = pyotp.TOTP(clean_secret)
        code = totp.now()
        time_remaining = 30 - (int(time.time()) % 30)
        print(f"[SUCCESS] TOTP Secret 有效！")
        print(f"当前 6 位验证码: {code} (剩余有效时间: {time_remaining}s)")
        return True
    except Exception as e:
        print(f"[ERROR] TOTP Secret 解析失败: {e}")
        return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        verify_totp(sys.argv[1])
    else:
        print("用法: python test_totp.py <YOUR_BASE32_TOTP_SECRET>")
