@echo off
setlocal
echo Running Echo-Core automated verification tests...
python tests/run_tests.py
if %ERRORLEVEL% equ 0 (
    echo.
    echo [PASS] All tests completed successfully.
) else (
    echo.
    echo [FAIL] Some tests encountered errors.
)
pause
