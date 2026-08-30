/** Built-in IMAP/SMTP presets carried over from the v1 account wizard. */
export const providerPresets = {
  gmail: {
    label: "Gmail",
    imapServer: "imap.gmail.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.gmail.com", smtpPort: 465, smtpSsl: true,
  },
  outlook: {
    label: "Outlook / Hotmail",
    imapServer: "outlook.office365.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp-mail.outlook.com", smtpPort: 587, smtpSsl: false,
  },
  microsoft365: {
    label: "Microsoft 365",
    imapServer: "outlook.office365.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.office365.com", smtpPort: 587, smtpSsl: false,
  },
  yahoo: {
    label: "Yahoo Mail",
    imapServer: "imap.mail.yahoo.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.mail.yahoo.com", smtpPort: 465, smtpSsl: true,
  },
  icloud: {
    label: "iCloud Mail",
    imapServer: "imap.mail.me.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.mail.me.com", smtpPort: 587, smtpSsl: false,
  },
  zoho: {
    label: "Zoho Mail",
    imapServer: "imap.zoho.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.zoho.com", smtpPort: 465, smtpSsl: true,
  },
  aol: {
    label: "AOL",
    imapServer: "imap.aol.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.aol.com", smtpPort: 465, smtpSsl: true,
  },
  gmx: {
    label: "GMX",
    imapServer: "imap.gmx.com", imapPort: 993, imapSsl: true,
    smtpServer: "mail.gmx.com", smtpPort: 465, smtpSsl: true,
  },
  qq: {
    label: "QQ 邮箱",
    imapServer: "imap.qq.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.qq.com", smtpPort: 465, smtpSsl: true,
  },
  netease: {
    label: "网易邮箱（163/126）",
    imapServer: "imap.163.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.163.com", smtpPort: 465, smtpSsl: true,
  },
  exmail: {
    label: "腾讯企业邮箱",
    imapServer: "imap.exmail.qq.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.exmail.qq.com", smtpPort: 465, smtpSsl: true,
  },
  alimail: {
    label: "阿里邮箱",
    imapServer: "imap.aliyun.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.aliyun.com", smtpPort: 465, smtpSsl: true,
  },
  yandex: {
    label: "Yandex",
    imapServer: "imap.yandex.com", imapPort: 993, imapSsl: true,
    smtpServer: "smtp.yandex.com", smtpPort: 465, smtpSsl: true,
  },
  linuxdo: {
    label: "Linux DO",
    imapServer: "mail.linux.do", imapPort: 993, imapSsl: true,
    smtpServer: "mail.linux.do", smtpPort: 465, smtpSsl: true,
  },
} as const;

export type ProviderPreset = keyof typeof providerPresets | "custom";
