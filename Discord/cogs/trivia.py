
from discord.ext import commands

import asyncio
import html
import re
import string

import aiohttp
from bs4 import BeautifulSoup
from pyparsing import Forward, Group, printables, OneOrMore, Suppress, Word, ZeroOrMore

from utilities import checks
from utilities.context import Context

def setup(bot):
	bot.add_cog(Trivia(bot))

class Trivia(commands.Cog):
	
	def __init__(self, bot):
		self.bot = bot
		self.wait_time = 15
		self.active = {}
		self.bot.loop.create_task(self.initialize_database())
	
	def cog_check(self, ctx):
		return checks.not_forbidden_predicate(ctx)
	
	async def initialize_database(self):
		await self.bot.connect_to_database()
		await self.bot.db.execute("CREATE SCHEMA IF NOT EXISTS trivia")
		await self.bot.db.execute(
			"""
			CREATE TABLE IF NOT EXISTS trivia.users (
				user_id		BIGINT PRIMARY KEY, 
				correct		INT,
				incorrect	INT,
				money		INT
			)
			"""
		)
	
	@commands.group(invoke_without_command = True)
	async def trivia(self, ctx):
		'''
		Trivia game
		Only your last answer is accepted
		Answers prepended with ! or > are ignored
		'''
		if ctx.guild.id in self.active:
			channel = ctx.guild.get_channel(self.active[ctx.guild.id]["channel_id"])
			return await ctx.embed_reply(f"There is already an ongoing game of trivia in {channel.mention}")
		self.active[ctx.guild.id] = {"channel_id": ctx.channel.id, "question_countdown": 0, "responses": {}}
		await self.trivia_round(ctx)
		del self.active[ctx.guild.id]
	
	@trivia.command(name = "bet")
	async def trivia_bet(self, ctx):
		'''
		Trivia with betting
		The category is shown first during the betting phase
		Enter any amount under or equal to the money you have to bet
		Currently, you start with $100,000
		'''
		if ctx.guild.id in self.active:
			channel = ctx.guild.get_channel(self.active[ctx.guild.id]["channel_id"])
			return await ctx.embed_reply(f"There is already an ongoing game of trivia in {channel.mention}")
		self.active[ctx.guild.id] = {"channel_id": ctx.channel.id, "question_countdown": 0, "responses": {}, 
										"bet_countdown": 0, "bets": {}}
		await self.trivia_round(ctx, bet = True)
		del self.active[ctx.guild.id]
	
	async def trivia_round(self, ctx, bet = False):
		try:
			async with ctx.bot.aiohttp_session.get("http://jservice.io/api/random") as resp:
				data = (await resp.json())[0]
		except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
			return await ctx.embed_reply(":no_entry: Error: Error connecting to API")
		if not data.get("question"):
			return await ctx.embed_reply(":no_entry: Error: API response missing question")
		if not data.get("category"):
			return await ctx.embed_reply(":no_entry: Error: API response missing category")
		# Add message about making POST request to API/invalid with id?
		# Include site page to send ^?
		if bet:
			self.active[ctx.guild.id]["bet_countdown"] = self.wait_time
			bet_message = await ctx.embed_say(None, title = string.capwords(data["category"]["title"]), 
												footer_text = f"You have {self.active[ctx.guild.id]['bet_countdown']} seconds left to bet")
			embed = bet_message.embeds[0]
			while self.active[bet_message.guild.id]["bet_countdown"]:
				await asyncio.sleep(1)
				self.active[bet_message.guild.id]["bet_countdown"] -= 1
				embed.set_footer(text = f"You have {self.active[bet_message.guild.id]['bet_countdown']} seconds left to bet")
				await bet_message.edit(embed = embed)
			embed.set_footer(text = "Betting is over")
			await bet_message.edit(embed = embed)
		self.active[ctx.guild.id]["question_countdown"] = self.wait_time
		question_message = await ctx.embed_say(data["question"], title = string.capwords(data["category"]["title"]), 
												footer_text = f"You have {self.active[ctx.guild.id]['question_countdown']} seconds left to answer")
		embed = question_message.embeds[0]
		while self.active[question_message.guild.id]["question_countdown"]:
			await asyncio.sleep(1)
			self.active[question_message.guild.id]["question_countdown"] -= 1
			embed.set_footer(text = f"You have {self.active[question_message.guild.id]['question_countdown']} seconds left to answer")
			try:
				await question_message.edit(embed = embed)
			except aiohttp.ClientConnectionError:
				continue
		embed.set_footer(text = "Time's up!")
		await question_message.edit(embed = embed)
		correct_players = []
		incorrect_players = []
		for player, response in self.active[ctx.guild.id]["responses"].items():
			if self.check_answer(data["answer"], response):
				correct_players.append(player)
			else:
				incorrect_players.append(player)
		if correct_players:
			correct_players_output = ctx.bot.inflect_engine.join([player.display_name for player in correct_players])
			correct_players_output += f" {ctx.bot.inflect_engine.plural('was', len(correct_players))} right!"
		else:
			correct_players_output = "Nobody got it right!"
		for correct_player in correct_players:
			await ctx.bot.db.execute(
				"""
				INSERT INTO trivia.users (user_id, correct, incorrect, money)
				VALUES ($1, 1, 0, 100000)
				ON CONFLICT (user_id) DO
				UPDATE SET correct = users.correct + 1
				""", 
				correct_player.id
			)
		for incorrect_player in incorrect_players:
			await ctx.bot.db.execute(
				"""
				INSERT INTO trivia.users (user_id, correct, incorrect, money)
				VALUES ($1, 0, 1, 100000)
				ON CONFLICT (user_id) DO
				UPDATE SET incorrect = users.incorrect + 1
				""", 
				incorrect_player.id
			)
		answer = BeautifulSoup(html.unescape(data["answer"]), "html.parser").get_text().replace("\\'", "'")
		await ctx.embed_say(f"The answer was `{answer}`", footer_text = correct_players_output)
		if bet and self.active[ctx.guild.id]["bets"]:
			bets_output = []
			for player, player_bet in self.active[ctx.guild.id]["bets"].items():
				if player in correct_players:
					difference = player_bet
					action_text = "won"
				else:
					difference = -player_bet
					action_text = "lost"
				money = await ctx.bot.db.fetchval(
					"""
					UPDATE trivia.users
					SET money = money + $2
					WHERE user_id = $1
					RETURNING money
					""", 
					player.id, difference
				)
				bets_output.append(f"{player.mention} {action_text} ${player_bet:,} and now has ${money:,}.")
			await ctx.embed_say('\n'.join(bets_output))
	
	def check_answer(self, answer, response):
		# Unescape HTML entities in answer and extract text between HTML tags
		answer = BeautifulSoup(html.unescape(answer), "html.parser").get_text()
		# Replace in answer: \' -> '
		# Replace: & -> and
		answer = answer.replace("\\'", "'").replace('&', "and")
		response = response.replace('&', "and")
		# Remove exclamation marks, periods, and quotation marks
		for character in '!."':
			if character in answer:
				answer = answer.replace(character, "")
			if character in response:
				response = response.replace(character, "")
		# Remove extra whitespace
		# Make lowercase
		answer = ' '.join(answer.split()).lower()
		response = ' '.join(response.split()).lower()
		# Remove article prefixes
		answer = self.remove_article_prefix(answer)
		response = self.remove_article_prefix(response)
		# Return False if empty response
		if not response:
			return False
		# Get items in lists
		answer_items = [item.strip() for item in answer.split(',')]
		answer_items[-1:] = [item.strip() for item in answer_items[-1].split("and") if item]
		response_items = [item.strip() for item in response.split(',')]
		response_items[-1:] = [item.strip() for item in response_items[-1].split("and") if item]
		# Check equivalence
		if set(answer_items) == set(response_items):
			return True
		# Check XX and YY ZZ
		last = answer_items[-1].split()
		if len(last) > 1:
			suffix = last[-1]
			if set([f"{item} {suffix}" for item in answer_items[:-1]] + [answer_items[-1]]) == set(response_items):
				return True
		last = response_items[-1].split()
		if len(last) > 1:
			suffix = last[-1]
			if set(answer_items) == set([f"{item} {suffix}" for item in response_items[:-1]] + [response_items[-1]]):
				return True
		# Remove commas
		if ',' in answer:
			answer = answer.replace(',', "")
		if ',' in response:
			response = response.replace(',', "")
		# Check for list separated by /
		if set(item.strip() for item in answer.split('/')) == set(item.strip() for item in response.split('/')):
			return True
		# Check removal of/replacement of - with space
		if answer.replace('-', ' ') == response.replace('-', ' '):
			return True
		if answer.replace('-', "") == response.replace('-', ""):
			return True
		# Check removal of parentheses
		if response == self.remove_article_prefix(answer.replace('(', "").replace(')', "")):
			return True
		# Check XX or YY
		if response in answer.split(" or "):
			return True
		# Check XX/YY
		if response in answer.split('/'):
			return True
		# Check XX/YY ZZ
		answer_words = answer.split()
		answers = answer_words[0].split('/')
		for answer_word in answer_words[1:]:
			if '/' in answer_word:
				answers = [f"{permutation} {word}" for permutation in answers for word in answer_word.split('/')]
			else:
				answers = [f"{permutation} {answer_word}" for permutation in answers]
		if response in answers:
			return True
		# Check numbers to words conversion
		response_words = response.split()
		for words in (answer_words, response_words):
			for index, word in enumerate(words):
				if word[0].isdigit():
					words[index] = self.bot.inflect_engine.number_to_words(word)
		if ' '.join(answer_words) == ' '.join(response_words):
			return True
		# Handle optional parentheses
		word = Word(printables, excludeChars = "()")
		token = Forward()
		token << ( word | Group(Suppress('(') + OneOrMore(token) + Suppress(')')) )
		expression = ZeroOrMore(token)
		parsed = expression.parseString(answer).asList()
		def add_accepted(accepted, item, initial_length = 0):
			if isinstance(item, list):
				accepted = add_optional_accepted(accepted, item)
			else:
				for accepted_index, accepted_item in enumerate(accepted[initial_length:]):
					accepted[initial_length + accepted_index] = f"{accepted_item} {item}".lstrip()
			return accepted
		def add_optional_accepted(accepted, optional):
			initial_length = len(accepted)
			if isinstance(optional[0], list):
				accepted = add_optional_accepted(accepted, optional[0])
			else:
				for accepted_item in accepted.copy():
					accepted.append(f"{accepted_item} {optional[0]}".lstrip())
			for item in optional[1:]:
				add_accepted(accepted, item, initial_length = initial_length)
			return accepted
		accepted = [""]
		for item in parsed:
			accepted = add_accepted(accepted, item)
		for item in parsed:
			if isinstance(item, list):
				accepted.extend(add_optional_accepted([""], item)[1:])
		for item in accepted:
			if item.startswith("or "):
				accepted.append(item[3:])
			if item.endswith(" accepted"):
				accepted.append(item[:-9])
		if response in accepted:
			return True
		# Check XX YY (or ZZ accepted)
		matches = re.search("(.+?)\s?\((?:or )?(.+)(?: accepted)?\)", answer)
		if matches and response == f"{matches.group(1).rsplit(' ', 1)[0]} {matches.group(2)}":
			return True
		# Check abbreviations
		for abbreviation, word in (("st", "saint"),):
			if (re.sub(fr"(^|\W)({abbreviation})($|\W)", fr"\1{word}\3", answer) == 
				re.sub(fr"(^|\W)({abbreviation})($|\W)", fr"\1{word}\3", response)):
				return True
		return False
	
	def remove_article_prefix(self, input):
		for article in ("a ", "an ", "the "):
			if input.startswith(article):
				return input[len(article):]
		return input
	
	@commands.Cog.listener()
	async def on_message(self, message):
		if not message.guild or message.guild.id not in self.active:
			return
		if message.channel.id != self.active[message.guild.id]["channel_id"]:
			return
		if self.active[message.guild.id].get("bet_countdown") and message.content.isdigit():
			ctx = await self.bot.get_context(message, cls = Context)
			money = await self.bot.db.fetchval("SELECT money FROM trivia.users WHERE user_id = $1", message.author.id)
			if not money:
				money = await self.bot.db.fetchval(
					"""
					INSERT INTO trivia.users (user_id, correct, incorrect, money)
					VALUES ($1, 0, 0, 100000)
					RETURNING money
					""", 
					message.author.id
				)
			if int(message.content) <= money:
				self.active[message.guild.id]["bets"][message.author] = int(message.content)
				await ctx.embed_reply(f"Has bet ${message.content}")
			else:
				await ctx.embed_reply("You don't have that much money to bet!")
		elif self.active[message.guild.id]["question_countdown"] and not message.content.startswith(('!', '>')):
			self.active[message.guild.id]["responses"][message.author] = message.content
	
	@trivia.command(name = "money", aliases = ["cash"])
	async def trivia_money(self, ctx):
		'''Trivia money'''
		money = await ctx.bot.db.fetchval("SELECT money FROM trivia.users WHERE user_id = $1", ctx.author.id)
		if not money:
			return await ctx.embed_reply("You have not played any trivia yet")
		await ctx.embed_reply(f"You have ${money:,}")
	
	@trivia.command(name = "score", aliases = ["points", "rank", "level"])
	async def trivia_score(self, ctx):
		'''Trivia score'''
		record = await ctx.bot.db.fetchrow("SELECT correct, incorrect FROM trivia.users WHERE user_id = $1", ctx.author.id)
		if not record:
			return await ctx.embed_reply("You have not played any trivia yet")
		total = record["correct"] + record["incorrect"]
		correct_percentage = record["correct"] / total * 100
		await ctx.embed_reply(f"You have answered {record['correct']}/{total} ({correct_percentage:.2f}%) correctly.")
	
	@trivia.command(name = "scores", aliases = ["scoreboard", "top", "ranks", "levels"])
	async def trivia_scores(self, ctx, number : int = 10):
		'''Trivia scores'''
		if number > 15:
			number = 15
		fields = []
		async with ctx.bot.database_connection_pool.acquire() as connection:
			async with connection.transaction():
				# Postgres requires non-scrollable cursors to be created
				# and used in a transaction.
				async for record in connection.cursor("SELECT * FROM trivia.users ORDER BY correct DESC LIMIT $1", number):
					# SELECT user_id, correct, incorrect?
					user = ctx.bot.get_user(record["user_id"])
					if not user:
						user = await ctx.bot.fetch_user(record["user_id"])
					total = record["correct"] + record["incorrect"]
					correct_percentage = record["correct"] / total * 100
					fields.append((str(user), f"{record['correct']}/{total} correct ({correct_percentage:.2f}%)"))
		await ctx.embed_reply(title = f"Trivia Top {number}", fields = fields)

