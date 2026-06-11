# How to Build & Install BibsAccountAdder

This is the **most reliable** way to add accounts to DreamBot. The script runs inside DreamBot and uses the official `AccountManager.addAccount()` API.

## What you need
- Java JDK (same one you use to build DreamBot scripts)
- Your existing DreamBot script project (or the DreamBot client JAR for compilation)

## Step 1: Compile the script

### Option A: Add to your existing project (easiest)
Copy `BibsAccountAdder.java` into your existing DreamBot script project next to your other scripts, e.g.:
```
dreambot-slayer/src/main/java/org/bibs/dreambot/BibsAccountAdder.java
```

Build your project as normal (Maven/Gradle/IDE). The compiled `.class` files will be in your output folder.

### Option B: Manual compile
Open a terminal in this folder and run:
```bash
javac -cp "%LOCALAPPDATA%\DreamBot\Client.jar" BibsAccountAdder.java
```

## Step 2: Package as JAR

If using your build tool (Maven/Gradle), it already produces a JAR. Otherwise manually:
```bash
jar cvf BibsAccountAdder.jar org/bibs/dreambot/BibsAccountAdder.class
```

## Step 3: Install into DreamBot

Copy the JAR to DreamBot's Scripts folder:
```
%LOCALAPPDATA%\DreamBot\Scripts\BibsAccountAdder.jar
```

Or if DreamBot loads from a different scripts path, put it there.

## Step 4: Use the Python app

1. Open `Bibs DreamBot Manager`
2. Select **"BibsAccountAdder (API)"** mode (should be default)
3. Paste your accounts and click **Parse**
4. Click **Add Accounts to DreamBot**
5. DreamBot will launch, run the script, add all accounts via the API, then stop itself

## How it works

The Python app:
1. Writes accounts to `%LOCALAPPDATA%\DreamBot\api_accounts.txt` in pipe format:
   ```
   nickname|email|password|pin|totp
   ```
2. Launches DreamBot via QuickStart:
   ```
   javaw -jar Client.jar -script "BibsAccountAdder" -params "api_accounts.txt"
   ```
3. The script reads the file and calls `AccountManager.addAccount()` for each line
4. Accounts appear immediately in DreamBot — no restart needed

## Troubleshooting

**"Script not found" in DreamBot console**
→ The JAR isn't in the right Scripts folder. Check where DreamBot loads scripts from.

**"ClassNotFoundException"**
→ The package path in the JAR doesn't match. Make sure it's `org/bibs/dreambot/BibsAccountAdder.class` inside the JAR.

**Accounts still not showing**
→ Check the DreamBot script console for error messages. The script logs every add attempt.
