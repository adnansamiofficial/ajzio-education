# AJZIO Education চালু করার সহজ নিয়ম (CachyOS / Linux)

## ১. কী লাগবে?

- Python 3.10 বা নতুন সংস্করণ
- Terminal
- Google sign-in ছাড়া বাকি ফিচার চালাতে কোনো API key, npm বা pip package লাগবে না।

```bash
python3 --version
```

## ২. ওয়েবসাইট চালু করো

ZIP Extract করে Terminal-এ:

```bash
cd ajzio-education
chmod +x run.sh
./run.sh
```

তারপর Browser-এ খোলো:

```text
http://127.0.0.1:4173
```

প্রথমে Login / Create Account স্ক্রিন আসবে। নতুন শিক্ষার্থী হলে **নতুন অ্যাকাউন্ট** বেছে নেবে।

বন্ধ করতে Terminal-এ `Ctrl + C` চাপবে।

## ৩. Signup-এ কী কী লাগবে?

- Display name — অন্য শিক্ষার্থীরা যে নামে চিনবে
- Email address
- Password এবং Confirm Password
- HSC Class XI অথবা Class XII
- Group — Science / Humanities / Business Studies
- Education Board
- পছন্দের ভাষা — বাংলা / English
- Community guidelines ও privacy notice-এ সম্মতি

নিরাপত্তা ও privacy-র জন্য ফোন নম্বর, বাসার ঠিকানা বা স্কুলের ID নেওয়া হয় না। Signup/Login-এর পর ছোট Welcome Survey আসবে। সেখানে পড়াশোনার লক্ষ্য, সবচেয়ে বড় সমস্যা এবং কোন ফিচার দরকার তা জানাতে পারবে।

## ৪. Google দিয়ে Login / Signup চালু করা

Google button তৈরি আছে। কিন্তু Google থেকে sign-in সত্যি চালাতে তোমার নিজের Google OAuth Client ID ও Secret লাগবে। এগুলো না দেওয়া পর্যন্ত Email/Password Login স্বাভাবিকভাবে কাজ করবে এবং Google button disabled থাকবে।

### Google Cloud Console-এ

1. Google Cloud Console-এ একটি project তৈরি/নির্বাচন করো।
2. OAuth consent screen configure করো।
3. OAuth Client ID তৈরি করো, type হবে **Web application**।
4. Authorized JavaScript origin হিসেবে দাও: `http://127.0.0.1:4173`
5. Authorized redirect URI হিসেবে দাও: `http://127.0.0.1:4173/auth/google/callback`
6. নিচের কমান্ডে নিজের credentials বসিয়ে Terminal থেকে চালাও:

```bash
export GOOGLE_CLIENT_ID='তোমার-client-id.apps.googleusercontent.com'
export GOOGLE_CLIENT_SECRET='তোমার-client-secret'
export GOOGLE_REDIRECT_URI='http://127.0.0.1:4173/auth/google/callback'
./run.sh
```

এগুলো কখনো GitHub-এ বা `app.js`-এ বসাবে না। Public website বানানোর সময় HTTPS domain-এর জন্য Google Console-এর origin ও callback URL আলাদা করে সেট করতে হবে। নতুন Google account-এ প্রথমবার প্রবেশের পর Class, Group ও Board বেছে নিলেই account সম্পূর্ণ হবে।

## ৫. নিজের Admin Account তৈরি

প্রথমবার Admin account তৈরি করার আগে server বন্ধ করো, তারপর:

```bash
HSC_ADMIN_EMAIL=তোমার-email@example.com ./run.sh
```

সাইটে গিয়ে ওই একই Email দিয়ে Signup করো। Account তৈরি হলে server বন্ধ করে স্বাভাবিকভাবে `./run.sh` চালাও।

আগে account খুলে ফেললে:

```bash
python3 server.py --promote তোমার-email@example.com
```

বিশ্বস্ত কাউকে Moderator করতে:

```bash
python3 server.py --make-moderator moderator@example.com
```

## ৬. Member ID ও Profile

- নতুন ডেটাবেসে প্রথম AJZIO Member ID হবে `1000`; এরপর `1001`, `1002` এভাবে সার্ভার নিজে নম্বর দেবে।
- আগের ডেটাবেসে থাকা account-গুলো migration-এর সময় তৈরি হওয়ার ক্রম অনুযায়ী নম্বর পাবে।
- Profile থেকে Display name, Class, Group, Board ও Language পরিবর্তন করা যাবে।
- Member ID ও permission role (Student / Moderator / Admin) পরিবর্তনের কোনো Profile option/API নেই।
- Public Profile-এ email বা password দেখানো হয় না।

## ৭. Community Chat ও নিরাপত্তা

- সব শিক্ষার্থী একটি shared academic chat room-এ পড়াশোনা নিয়ে কথা বলতে পারবে। শিক্ষার্থীদের নিরাপত্তার জন্য private DM নেই।
- Owner/Admin-এর দৃষ্টি আকর্ষণ করতে `@admin` লিখে মেসেজ পাঠাও। Admin Panel খোলা থাকলে এটি প্রতি ১৫ সেকেন্ডে নতুন মেসেজ/মেনশন দেখে। এটি in-app alert; SMS বা ফোনের push notification নয়।
- ফোন নম্বর, ইমেইল এবং পরিচিত abusive শব্দের মতো মেসেজ সার্ভার পোস্টের আগেই আটকে দেয়; এমন মেসেজ chat database-এ সংরক্ষণ করা হয় না। সাধারণ chat message ৩০ দিন পর মুছে যায়।
- প্রথম detected নিয়মভঙ্গে সতর্কতা, সাত দিনের মধ্যে দ্বিতীয়বার হলে ৬ ঘণ্টা, এরপরের নিয়মভঙ্গে ২৪ ঘণ্টার suspension।
- Admin প্রয়োজনে ৬/২৪ ঘণ্টা suspend অথবা account lock করতে পারবে।
- Account lock/suspend হলে সাধারণ ওয়েবসাইট ব্যবহার বন্ধ হয়ে Appeal পেজ আসবে। শিক্ষার্থী সেখানে ব্যাখ্যা দিয়ে আপিল পাঠাতে পারবে। Admin Panel থেকে আপিল দেখে Approve করলে account unlock হবে।
- Chat filter একটি প্রাথমিক automatic safety filter; সব ধরনের খারাপ/এড়িয়ে যাওয়া ভাষা ধরতে পারবে এমন নিশ্চয়তা নেই। তাই Report option এবং Admin moderation-ও আছে।

## ৮. Admin কী করতে পারবে?

- শিক্ষার্থীদের জমা দেওয়া নোট Review / Approve / Reject করা
- সংশোধনের জন্য নোট ফেরত দেওয়া
- শিক্ষার্থীদের ভুল/সমস্যার Report দেখা
- শিক্ষার্থীদের Feature Request ও Feedback পর্যালোচনা করা
- Feedback-কে In Review বা Done হিসেবে চিহ্নিত করা
- নতুন MCQ Exam তৈরি ও প্রকাশ করা

একজন Moderator/Admin নিজের জমা দেওয়া নোট নিজে Approve করতে পারবে না।

Admin Panel-এ শিক্ষার্থী account খোঁজা, ৬/২৪ ঘণ্টা suspend, lock/unlock, appeal review, chat report, `@admin` mention, moderation log এবং Admin Audit History দেখা যাবে। Admin-এর গুরুত্বপূর্ণ lock/suspend/unlock, appeal decision ও chat deletion action Audit History-তে লেখা হয়।

## ৯. শিক্ষার্থীদের Feedback

Login-এর পর Sidebar থেকে **তোমার মতামত** পেজে যাবে। সেখানে:

- নতুন Feature Idea
- Bug Report
- Note / Question Correction
- Other Suggestion

পাঠাতে পারবে। নিজের পাঠানো মতামতের Status-ও দেখতে পারবে। দিনে সর্বোচ্চ পাঁচটি Feedback পাঠানো যাবে।

## ১০. ডেটা কোথায় থাকবে?

`data/` ফোল্ডারে:

- `hsc-circle.sqlite3` — অ্যাকাউন্ট, স্থায়ী Member ID, Profile, chat, appeal, moderation/admin audit, নোট, পরীক্ষার ফল, Survey ও Feedback
- `uploads/` — PDF ও ছবি

এই `data/` ফোল্ডার কখনো মুছে ফেলবে না। নিয়মিত Backup রাখবে। আগের HSC Circle project-এর `data/` থাকলে নতুন প্রজেক্টে প্রথমবার চালানোর আগে সেটি কপি করলে পুরোনো account ও note থাকবে।

## ১১. অন্য ডিভাইস থেকে চালিয়ে দেখা

নিজের trusted Wi-Fi network-এ পরীক্ষা করতে:

```bash
HSC_HOST=0.0.0.0 PORT=4173 ./run.sh
```

তারপর নিজের PC-এর LAN IP দিয়ে অন্য ডিভাইসে খোলো। এই development HTTP server-কে সরাসরি public internet-এ প্রকাশ করবে না।

## ১২. সবার জন্য ফ্রি

Signup, Login, Notes, Downloads, Weekly Exams, Results, Answer Explanations, Note Requests এবং Feedback—সব শিক্ষার্থীর জন্য বিনামূল্যে। কোনো Premium বা Subscription নেই।

Public launch করতে হলে VPS/hosting, domain, HTTPS, email verification, password recovery, upload malware scanning, backup ও security review-এর কাজ বাকি থাকবে।

## ১৩. Profile, Mention ও Chat Update

- প্রতিটি শিক্ষার্থীর আলাদা Profile page আছে। নিজের Profile থেকে ছবি (PNG/JPG/WebP, সর্বোচ্চ ২ MB), Bio (সর্বোচ্চ ২৮০ অক্ষর), শ্রেণি, বিভাগ, বোর্ড ও ভাষা বদলানো যায়। AJZIO Member ID এবং Account Role স্থায়ী—এগুলো edit করা যায় না।
- জন্মতারিখ ঐচ্ছিক। দিলে সেটি ডিফল্টভাবে private থাকে; Profile Edit-এ আলাদা সম্মতি দিয়ে Public করতে হয়। Public Profile API-ও private জন্মতারিখ প্রকাশ করে না।
- Chat-এ কারও নাম, ছবি বা `#MemberID` চাপলে তার Profile দেখা যাবে। `Reply` দিয়ে নির্দিষ্ট মেসেজের উত্তর দেওয়া যায়।
- `@admin`, `@moderator`, `@student` অথবা `@MemberID` লিখলে Mention suggestion আসবে। Role mention message-এ দৃশ্যমান থাকবে। Admin inbox-এ শুধু `@admin`/Admin mention আসবে—পুরো chat history নয়।
- যেকোনো অন্য সদস্যের message থেকে `Report` করা যায়। Admin Panel-এর Chat Reports-এ কারণসহ রিপোর্ট দেখা ও review/dismiss করা যায়। নিজের message নিজে report করা যায় না এবং একই message-এ একই account থেকে duplicate report নেওয়া হয় না।
- Bangla/Banglish-এ পরিচিত কিছু অশালীন শব্দ, বানান-ভ্যারিয়েন্ট ও আলাদা অক্ষর লিখে এড়ানোর প্রচেষ্টা আটকানোর basic filter আছে। তবে এটি পূর্ণাঙ্গ ভাষা-বোঝার ব্যবস্থা নয়—নতুন slang, ভুল বানান বা ইচ্ছাকৃত evasion সবসময় ধরতে পারবে না; কখনো নিরীহ শব্দও ভুল করে আটকে যেতে পারে। তাই Report ও Admin review ব্যবহার করতে হবে।
- `@moderator` বা `@student` role mention বর্তমানে message-এ tag হিসেবে দেখায়; আলাদা push/SMS alert পাঠায় না।
