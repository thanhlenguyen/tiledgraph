import asyncio

async def fetch_data(id, delay):
    print(f"Task {id}: starting...")
    await asyncio.sleep(delay)  # Non-blocking sleep
    print(f"Task {id}: done!")
    return f"Data {id}"

async def main():
    # Run multiple async tasks concurrently
    results = await asyncio.gather(
        fetch_data(1, 2),
        fetch_data(2, 1),
        fetch_data(3, 3)
    )
    print("All results:", results)

# Entry point to run the event loop
asyncio.run(main())