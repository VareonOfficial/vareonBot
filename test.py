import asyncio
from telethon import TelegramClient

async def main():
    print("=== Telethon Interactive Session Generator ===\n")
    
    # Prompting for your Telegram Developer API details
    # Get these from https://telegram.org
    api_id_input = input("Enter your API ID (numbers only): ").strip()
    api_id = int(api_id_input)
    
    api_hash = input("Enter your API HASH: ").strip()
    session_name = input("Enter output session name (default: temp_session): ").strip() or "temp_session"

    print(f"\nConnecting to Telegram and initializing '{session_name}.session'...")
    
    # This automatically prompts for phone number, login code, and 2FA password in the terminal
    client = TelegramClient(session_name, api_id, api_hash)
    
    async with client:
        me = await client.get_me()
        print("\n" + "="*40)
        print(" SUCCESSFUL LOGIN!")
        print(f" Account: {me.first_name} [@{me.username or 'No Username'}]")
        print(f" File Saved As: {session_name}.session")
        print("="*40)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError:
        print("\nError: API ID must be a number. Please try again.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
