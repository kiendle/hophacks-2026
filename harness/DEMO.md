# Signal, the demo script

One app, one address: **http://127.0.0.1:5194**. The Morning Brief page is the home page and the
assistant lives in the chat bubble at the bottom right of it.

## Before you start

1. Open a terminal in the project folder and run `python -m uv run harness/signal_server.py`.
2. Wait for the line that says it is running on 127.0.0.1:5194.
3. Open http://127.0.0.1:5194 in the browser. The page should say how many posts have been kept.
4. Click the round chat bubble at the bottom right to open the assistant.

To stop it, go back to that terminal and press Ctrl and C together. Only one copy can run at a time.
If it refuses to start and says the port is in use, a copy is already running, so use that one.

## The two questions, in this order

### 1. "How did people react to the Anthropic resignation post around Sep 9?"

Takes about 15 to 20 seconds.

On screen: four steps appear one under the other, each with a details link. They read like
"Checking which finished projects we have", "Looking at the feeling chart", "Reading 15 of the most
liked posts from Sep 9". Then a chart card appears with a picture of how the feeling moved day by
day, and under it the answer, which names Sep 9 as the darkest day and quotes real posts.

While it works, say: every step is a real tool call against a real archive of a month of posts, and
you can open any step to see exactly what was asked and what came back.

### 2. "Brief me on AI in one minute."

The answer comes back in about 10 seconds and the recording is ready about a minute later.

On screen: two steps, then a card that says "Your audio brief" and keeps you posted while it works.
It reads "Choosing the stories", then "Writing the script", then "Recording the voice". When it is
done the card shows a play button and the list of stories. Press play.

While it waits, say: it is reading the posts collected live from Bluesky tonight, picking the
stories people actually reacted to, writing the script and having it read aloud. The same brief is
on the home page behind the chat.

## If something goes wrong

- **A step turns red.** Read the one line it shows and ask the question again in simpler words.
- **The brief card says it is taking longer than usual.** Ask "how is my brief doing" in the chat.
- **The chart has no picture.** The numbers under it are still real. Carry on with those.
- **Nothing happens at all.** Check the terminal is still running. If it is not, start it again with
  the command above and reload the page. The chat starts a fresh conversation.
- **Worst case.** Reload the page and ask the question again. Nothing is lost.

## Telegram

The same assistant answers on Telegram, and the Hopkins network blocks Telegram. Put the laptop on a
phone hotspot before showing that part, or skip it and stay on the website.

## Good to know if someone asks

- The assistant runs on this laptop. It has no tools except ours, so it cannot read files or run
  commands here.
- Nothing is submitted without the Confirm button. The model cannot press it.
- The posts are what people wrote on social media, not checked reporting.
