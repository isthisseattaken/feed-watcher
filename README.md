# Feed Watcher

Builds one RSS feed from every source in `sources.yml`, twice a day, for free on GitHub. Make then watches that single feed, filters it by keyword, and sends you a digest.

```
sources.yml ──▶ build_feed.py (runs on GitHub twice a day) ──▶ docs/feed.xml ──▶ Make ──▶ your inbox
```

## What's in here

| File | What it does | Do you edit it? |
|---|---|---|
| `sources.yml` | Your list of sources and the feed's name | **Yes**, this is the one |
| `build_feed.py` | Reads each source and writes the merged feed | No |
| `.github/workflows/build-feed.yml` | Tells GitHub when to run the script | Only to change the schedule |
| `requirements.txt` | The three libraries the script needs | No |
| `docs/` | The finished feed and a simple reading page (created by the script) | No |
| `data/seen.json` | Memory of every item seen, so nothing repeats (created by the script) | No |

## One-time setup

1. **Make a GitHub account** at github.com with your personal email.
2. **Create a new repository.** Click **+** (top right) → **New repository**. Name it `feed-watcher`, set it to **Public** (needed for free scheduled runs), tick **Add a README file**, and create it.
3. **Upload the files.** On the repository page, click **Add file → Upload files** and drag in `sources.yml`, `build_feed.py`, `requirements.txt` and this `README.md`. Click **Commit changes**.
4. **Add the schedule file.** Folders starting with a dot often get skipped when dragging, so add this one by hand: **Add file → Create new file**, type the name `.github/workflows/build-feed.yml` (the slashes create the folders), paste in the contents of that file, and commit.
5. **Set your feed's address.** Open `sources.yml`, click the pencil icon, and replace `YOUR-USERNAME` with your GitHub username. Commit.
6. **Run it once.** Go to the **Actions** tab → **Build feed** → **Run workflow**. After a minute or two you should see a green check, and new `docs/` and `data/` folders in the repository.
7. **Publish the feed.** Go to **Settings → Pages**. Under *Build and deployment*, choose **Deploy from a branch**, branch **main**, folder **/docs**, and save. After a few minutes your feed is live at:

   `https://YOUR-USERNAME.github.io/feed-watcher/feed.xml`

   and a simple reading page at `https://YOUR-USERNAME.github.io/feed-watcher/`.
8. **Check it.** Paste the feed address into validator.w3.org/feed.

From then on, it runs by itself twice a day.

## Adding a source

**If the site already has a feed** (see the "Finding a feed" checks in your learning doc), add:

```yaml
  - label: Some Blog
    type: feed
    url: https://someblog.com/feed/
```

**If it doesn't**, you write two rules that pick out the article links:

1. Open the page that lists the articles and look at a few article addresses. Find the part they all start with, like `https://site.com/blog/`. That's `link_starts_with`.
2. Hover over the *other* links on the page that start the same way but aren't articles (tag pages, author pages, "page 2"). Add the distinctive bit of each to `skip_if_contains`.
3. Optional: if the page has article links in a sidebar or footer you don't want, right-click an article headline → **Inspect**, find a class on the box that holds the list, and add a CSS selector like `selector: ".post-list a"`. This is the same CSS you'd write for styling.

```yaml
  - label: Some Magazine
    type: page
    url: https://somemagazine.com/articles
    link_starts_with: https://somemagazine.com/articles/
    skip_if_contains:
      - /articles/tag/
      - /articles/page/
```

Edit `sources.yml` on GitHub, commit, and the next run picks it up. To test right away, use **Run workflow** again and check the run's log: each source shows how many links it found and how many were new.

## Hooking it up to Make

In Make, start a scenario with the **RSS → Watch RSS feed items** trigger and paste in your `feed.xml` address. When it asks where to start, choose to start from now on, so you don't get every existing item at once. Each item has:

- **Title** and **URL** of the article
- **Description** saying which source it came from (`From Current Affairs`)
- **Category** set to the source's label

One scenario covers every source, which keeps you inside Make's free plan.

## Ground rules built in

- It checks each site's `robots.txt` and skips any page the site asks automated tools not to read.
- It waits 2 seconds between requests and runs only twice a day.
- One broken source doesn't stop the others. If **every** source fails, the run turns red and GitHub emails you.

## When something goes wrong

- **A source says "0 links found".** The site probably changed its layout or addresses. Open the page, look at the article links again, and update its rules.
- **The run fails at "Save the updated feed" with a permission error.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and save.
- **It stopped running on its own.** GitHub pauses scheduled runs in public repositories after 60 days with no activity. The script's own updates should count as activity, but if it ever pauses, the Actions tab will show a button to turn it back on.
