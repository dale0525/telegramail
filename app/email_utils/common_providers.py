"""
Stores configurations for common email providers.
"""

COMMON_PROVIDERS = [
    {
        "name": "Gmail",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,  # Use 465 for SSL, 587 for TLS
        "smtp_ssl": True,  # Gmail typically uses STARTTLS on port 587
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "imap_ssl": True,
    },
    {
        "name": "Outlook/Hotmail",
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_ssl": True, # Outlook uses STARTTLS on port 587
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_ssl": True,
    },
    # Add more providers here
    # {
    #     "name": "Yahoo Mail",
    #     "smtp_server": "smtp.mail.yahoo.com",
    #     "smtp_port": 465, # Requires SSL
    #     "smtp_ssl": True,
    #     "imap_server": "imap.mail.yahoo.com",
    #     "imap_port": 993,
    #     "imap_ssl": True,
    # },
    # {
    #     "name": "iCloud Mail",
    #     "smtp_server": "smtp.mail.me.com",
    #     "smtp_port": 587, # Requires TLS
    #     "smtp_ssl": True,
    #     "imap_server": "imap.mail.me.com",
    #     "imap_port": 993,
    #     "imap_ssl": True,
    # },
] 