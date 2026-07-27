Small Project, written on 1 Day ~12 Hours work from Scratch to Status quo.

It can fetch data from WKO and Firmenabc (they are more protected tho)

my first Iteration was health_leads_RNGDELAY that i debugged a lot, main issues being in the pagination (load more button) and worked fine but only scraped from WKO.at


i reworked the code after that since i wanted to add more Websites, and putting all into 1 Single piece of .py file is just too much.

scraper_Core as main scraper function and wko.py + firmenabc1.py as site adjusted crawlers.

(Due to the sites having different Indexes/Layouts etc.)

Functions without Bugs as of 26.07

firmenabc1 wasn't hard tested only first 20 entrys. worked fine.

Additional wanted to add's:
Scraper for Herold, aswell saving in an autonomous csv.
Merger to Gather all the data across the different csv files, and Combine them into a complete list.

The wko.py scraper checks for all columns in the csv, if they exist, he Gathers the data, saves it 1 by 1 into the csv so if it crashes, the data isn't lost, and you can continue after Restart.
First It will gather all the links (100), perform "Load more" button press, checks if there are already existent Links in the CSV,
If yes -> Skip, it will fetch the remaining ones, and asks the user after 100 if he wants to continue and repeats the Processus.


It checks the website of each individual business and Judges the Website based on:

Https
Viewport to check if Looks accurately in mobile
Copyright and flags if below 2020
CMS, flags only if it was Hand built or by something way out of date.
Tables in case it was built in the early commercial web pre "modern" css 2010+.


And gives it a score if Website is outdated 0-5 (-1 If unreachable)
Should work fine, Viewport Https, CMS and Copyright working fine, didnt find cases of miss-used tables.


firmenabc has intentionally a bit longer delay due to a faster detection than WKO, recommend only scraping for About 10-15 minutes per day or you Risk lockout/ban.



DO NOT SCRAPE MORE THAN 1000 in 1 DAY on WKO, you will get a hard delay or likely a soft ban.
firmenabc 

Check code for more Details, may not be 100% since i didn't hand-code it, just architectured it without Looking too close.

Used Tools: Cursor and Claude
Used Programs: Python with playwright, requests and beautifulsoup4

Additionally i built a quick html Website with Claude, incl. working web3 email form (sends me a real email) and an additional Impressum site. 

Greetings.
