# IPv6Monitor

نمایش زنده و جداگانه ترافیک **IPv4** و **IPv6** در اوبونتو، همراه با ذخیره
پایدار مجموع مصرف و تاریخچه ترافیک.

این پروژه با `nftables` بسته‌های IPv4 و IPv6 را روی کارت شبکه شمارش می‌کند، یک
سرویس systemd دائمی اجرا می‌کند و اطلاعات تجمعی را در SQLite نگه می‌دارد. بستن
ترمینال، ری‌استارت سرویس یا ریبوت سرور باعث صفرشدن مجموع مصرف نمی‌شود.

## قابلیت‌ها

- نمایش لحظه‌ای سرعت دانلود و آپلود برای IPv4 و IPv6
- نمایش سرعت شبکه با واحد خوانای `Kbit/s`، `Mbit/s` یا `Gbit/s`
- به‌روزرسانی پیش‌فرض هر `1` ثانیه
- تشخیص خودکار کارت شبکه دارای Default Route
- اجرای ساده با فرمان `ipv6monitor`
- نصب کامل با یک دستور
- نصب خودکار `python3`، `nftables`، `iproute2` و پیش‌نیازها
- ساخت، فعال‌سازی و اجرای خودکار سرویس systemd
- اجرای مجدد سرویس پس از خطا و راه‌اندازی خودکار پس از روشن‌شدن سرور
- ذخیره مجموع مصرف و تاریخچه در SQLite
- نگهداری پیش‌فرض ۳۰ روز تاریخچه تجمیع‌شده
- ارتقای idempotent بدون حذف تنظیمات و دیتابیس قبلی
- حذف امن با امکان نگه‌داشتن یا پاک‌کردن داده‌ها

## نصب تک‌خطی

```bash
curl -fsSL https://raw.githubusercontent.com/HamedSanaei/ipv6monitor/main/install.sh | sudo bash
```

پس از پایان نصب، مانیتور را اجرا کنید:

```bash
ipv6monitor
```

در خروجی برنامه، `Download` به معنی ترافیک دریافت‌شده توسط سرور و `Upload`
به معنی ترافیک ارسال‌شده از سرور است. برای جلوگیری از شلوغی، سرعت فقط با واحد
شبکه (`Kbit/s`، `Mbit/s` یا `Gbit/s`) نمایش داده می‌شود.

> برای انتشار رسمی، بهتر است به‌جای `main` از یک Tag ثابت مانند `v1.1.0` در
> دستور نصب استفاده شود تا محتوای نصب در آینده بدون تغییر باقی بماند.

## دستورات

نمایش زنده:

```bash
ipv6monitor
```

نمایش یک Snapshot:

```bash
ipv6monitor status
```

خروجی JSON:

```bash
ipv6monitor status --json
```

خلاصه تاریخچه ۲۴ ساعت اخیر:

```bash
ipv6monitor history --hours 24
```

وضعیت سرویس systemd:

```bash
ipv6monitor service-status
```

ریست کامل آمار ذخیره‌شده و شروع دوباره شمارش:

```bash
sudo ipv6monitor reset
```

## مدیریت سرویس

```bash
sudo systemctl status ipv6monitor
sudo systemctl restart ipv6monitor
sudo systemctl stop ipv6monitor
sudo systemctl start ipv6monitor
sudo journalctl -u ipv6monitor -f
```

سرویس هنگام نصب با دستور معادل زیر فعال می‌شود و پس از هر Boot اجرا خواهد شد:

```bash
sudo systemctl enable --now ipv6monitor
```

## تنظیمات

فایل تنظیمات:

```text
/etc/ipv6monitor/ipv6monitor.conf
```

مقادیر پیش‌فرض:

```ini
INTERFACE=auto
REFRESH_INTERVAL=1
SAVE_INTERVAL=10
HISTORY_INTERVAL=60
HISTORY_RETENTION_DAYS=30
RUNTIME_DIR=/run/ipv6monitor
STATE_DIR=/var/lib/ipv6monitor
```

### انتخاب دستی کارت شبکه

ابتدا نام کارت‌های شبکه را ببینید:

```bash
ip -br link
ip route show default
```

سپس فایل تنظیمات را ویرایش کنید:

```bash
sudo nano /etc/ipv6monitor/ipv6monitor.conf
```

برای نمونه:

```ini
INTERFACE=ens3
```

و سرویس را ری‌استارت کنید:

```bash
sudo systemctl restart ipv6monitor
```

## ذخیره پایدار اطلاعات

اطلاعات پایدار در فایل زیر ذخیره می‌شود:

```text
/var/lib/ipv6monitor/traffic.db
```

دیتابیس شامل این موارد است:

- مجموع دائمی بایت‌ها و تعداد Packetهای IPv4 RX/TX
- مجموع دائمی بایت‌ها و تعداد Packetهای IPv6 RX/TX
- نمونه‌های تجمیع‌شده تاریخچه با فاصله پیش‌فرض یک دقیقه

Snapshot لحظه‌ای برای رابط خط فرمان در مسیر زیر قرار می‌گیرد:

```text
/run/ipv6monitor/status.json
```

فایل داخل `/run` پس از ریبوت موقتاً حذف می‌شود، اما منبع اصلی اطلاعات نیست.
سرویس پس از Boot دیتابیس SQLite را می‌خواند و شمارش را از مجموع قبلی ادامه می‌دهد.

`SAVE_INTERVAL=10` یعنی مجموع مصرف حداکثر هر ۱۰ ثانیه در دیتابیس Commit می‌شود.
هنگام Stop یا Restart عادی سرویس، آخرین Counterها نیز قبل از خروج ذخیره می‌شوند.
در خاموشی ناگهانی برق ممکن است حداکثر به‌اندازه این بازه، آخرین ترافیک ثبت‌نشده
از دست برود. برای کاهش این پنجره می‌توانید مقدار را کمتر کنید، ولی نوشتن روی دیسک
بیشتر خواهد شد.

## روش شمارش

پروژه یک Table اختصاصی با نام زیر می‌سازد:

```text
inet ipv6monitor
```

چهار قانون فقط-شمارنده ایجاد می‌شود:

- IPv4 RX روی `prerouting`
- IPv6 RX روی `prerouting`
- IPv4 TX روی `postrouting`
- IPv6 TX روی `postrouting`

این قوانین هیچ Packetی را Drop، Reject، Redirect یا NAT نمی‌کنند و فایل دائمی
`/etc/nftables.conf` را تغییر نمی‌دهند. Table پروژه هنگام شروع سرویس بازسازی
می‌شود و هنگام خروج عادی حذف می‌شود.

## نصب از Clone محلی

```bash
git clone https://github.com/HamedSanaei/ipv6monitor.git
cd ipv6monitor
sudo bash install.sh
```

نصب‌کننده تشخیص می‌دهد که فایل‌ها محلی هستند و همان فایل‌های Clone را نصب می‌کند.

## تست توسعه

```bash
python3 -m py_compile src/ipv6monitor.py
python3 -m unittest discover -s tests -v
bash -n install.sh
bash -n uninstall.sh
```

Smoke test روی یک ماشین Ubuntu دارای systemd:

```bash
sudo bash install.sh
systemctl is-active --quiet ipv6monitor
sudo nft list table inet ipv6monitor
ipv6monitor status
sudo systemctl restart ipv6monitor
ipv6monitor status
```

## حذف

حذف برنامه با نگه‌داشتن تنظیمات و دیتابیس:

```bash
sudo ipv6monitor-uninstall
```

حذف کامل برنامه، تنظیمات و تمام تاریخچه:

```bash
sudo ipv6monitor-uninstall --purge
```

## مسیر فایل‌های نصب‌شده

```text
/usr/local/bin/ipv6monitor
/usr/local/lib/ipv6monitor/ipv6monitor.py
/usr/local/sbin/ipv6monitor-uninstall
/etc/ipv6monitor/ipv6monitor.conf
/etc/systemd/system/ipv6monitor.service
/var/lib/ipv6monitor/traffic.db
/run/ipv6monitor/status.json
```

## سازگاری

- Ubuntu و توزیع‌های Debian-based دارای `apt-get`
- systemd
- Python 3.10 یا جدیدتر
- nftables
- دسترسی Root برای نصب و Collector

نمایش اطلاعات با فرمان `ipv6monitor` نیاز به Root ندارد؛ سرویس Collector با حداقل
Capabilityهای لازم برای دسترسی به nftables اجرا می‌شود.

## محدودیت‌ها

- ترافیک بر اساس Hookهای IP در Netfilter شمارش می‌شود؛ سربار لایه Ethernet در
  مقدارها وجود ندارد.
- ترافیک RX شامل Packetهایی است که وارد Interface شده‌اند، حتی اگر بعداً توسط
  Firewall دیگری Drop شوند.
- اگر Table پروژه به‌صورت دستی حذف شود، سرویس آن را بازسازی می‌کند، اما ترافیک
  بین حذف و بازسازی قابل بازیابی نیست.
- در سیستم‌هایی با چند Default Route، Interface دارای Metric کمتر انتخاب می‌شود.

## مجوز

MIT
