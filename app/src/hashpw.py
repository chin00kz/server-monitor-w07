import getpass, os, sys
from argon2 import PasswordHasher

pw = getpass.getpass("New dashboard password (min 12 chars): ")
if len(pw) < 12 or pw != getpass.getpass("Repeat it: "):
    sys.exit("Too short or not matching. Nothing saved.")
fd = os.open("/secrets/admin.hash", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(PasswordHasher().hash(pw))
print("Saved /secrets/admin.hash")
