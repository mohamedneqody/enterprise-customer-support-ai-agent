@echo off
chcp 65001 >nul
title تشغيل نظام الدعم الذكي (AI Support Agent)

echo ========================================================
echo        مرحباً بك في نظام الدعم الذكي (All-In-One AI)
echo ========================================================
echo.

:: سؤال المستخدم عن اسم المتجر/الكيان
set /p STORE_NAME="أدخل اسم المتجر أو الكيان (اضغط Enter لاستخدام 'متجر النور' كافتراضي): "
if "%STORE_NAME%"=="" set STORE_NAME=متجر النور

echo.
echo سيتم تشغيل النظام باسم: %STORE_NAME%
echo جاري تدريب النظام على أحدث الملفات...
python train.py

echo.
echo جاري تشغيل السيرفر...
python app.py

pause
