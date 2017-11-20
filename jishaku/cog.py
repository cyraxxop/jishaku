# -*- coding: utf-8 -*-

from . import utils

import discord
from discord.ext import commands

import asyncio
import re
import subprocess
import time
import traceback
import typing


class Jishaku:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.init_time = time.monotonic()
        self.repl_global_scope = {}
        self.repl_local_scope = {}

    @staticmethod
    async def do_after_sleep(delay: float, coro, *args, **kwargs):
        await asyncio.sleep(delay)
        return await coro(*args, **kwargs)

    def do_later(self, delay: float, coro, *args, **kwargs):
        return self.bot.loop.create_task(self.do_after_sleep(delay, coro, *args, **kwargs))

    @commands.group(name="jishaku", aliases=["jsk"])
    @commands.is_owner()
    async def jsk(self, ctx):
        """Jishaku debug and diagnostic commands

        This command on its own does nothing, all functionality is in subcommands.
        """

        pass

    @jsk.command(name="selftest")
    async def self_test(self, ctx):
        """Jishaku self-test

        This tests that Jishaku and the bot are functioning correctly.
        """

        current_time = time.monotonic()
        time_string = utils.humanize_relative_time(self.init_time - current_time)
        await ctx.send(f"Jishaku running, init {time_string}.")

    def prepare_environment(self, ctx: commands.Context):
        """Update the REPL scope with variables relating to the current ctx"""
        self.repl_global_scope.update({
            "_bot": ctx.bot,
            "asyncio": asyncio,
            "discord": discord
        })

    @jsk.command(name="python", aliases=["py", "```py"])
    async def python_repl(self, ctx, *, code: str):
        """Python REPL-like command

        This evaluates or executes code passed into it, supporting async syntax.
        Global variables include _ctx and _bot for interactions.
        """

        code = utils.cleanup_codeblock(code)
        await self.repl_backend(ctx, code)

    async def repl_backend(self, ctx: commands.Context, code: str):
        """Attempts to compile code and execute it."""
        # create handle that'll add a right arrow reaction if this execution takes a long time
        handle = self.do_later(1, self.attempt_add_reaction, ctx.message, "\N{BLACK RIGHT-POINTING TRIANGLE}")

        if "\n" not in code:
            # if there are no line breaks try eval mode first
            with_return = ' '.join(['return', code])

            try:
                # try to compile with 'return' in front first
                # this lets you do eval-like expressions
                coro_format = utils.repl_coro(with_return)
                code_object = compile(coro_format, '<repl-v session>', 'exec')
            except SyntaxError:
                code_object = None
        else:
            code_object = None

        # we set as None and check here because nesting looks worse and complicates the traceback
        # if this code fails.

        if code_object is None:
            try:
                coro_format = utils.repl_coro(code)
                code_object = compile(coro_format, '<repl-x session>', 'exec')
            except SyntaxError as exc:
                handle.cancel()
                await self.attempt_add_reaction(ctx.message, "\N{HEAVY EXCLAMATION MARK SYMBOL}")
                await self.repl_handle_syntaxerror(ctx, exc)
                return

        # our code object is ready, let's actually execute it now
        self.prepare_environment(ctx)

        try:
            exec(code_object, self.repl_global_scope, self.repl_local_scope)

            # Grab the coro we just defined
            extracted_coro = self.repl_local_scope.get("__repl_coroutine")

            # Await it with local scope args
            result = await extracted_coro(ctx)
        except Exception as exc:
            handle.cancel()
            await self.attempt_add_reaction(ctx.message, "\N{DOUBLE EXCLAMATION MARK}")
            await self.repl_handle_exception(ctx, exc)
        else:
            if result is None:
                handle.cancel()
                await self.attempt_add_reaction(ctx.message, "\N{WHITE HEAVY CHECK MARK}")
                return

            if not isinstance(result, str):
                # repr all non-strings
                result = repr(result)

            # if result is really long cut it down
            if len(result) > 1995:
                result = result[0:1995] + "..."
            handle.cancel()
            await ctx.send(result)
            await self.attempt_add_reaction(ctx.message, "\N{WHITE HEAVY CHECK MARK}")

    @staticmethod
    async def repl_handle_exception(ctx, exc: Exception):
        """Handles exec exceptions.

        This tries to DM the author with the traceback."""
        traceback_content = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, 8))
        await ctx.author.send(f"```py\n{traceback_content}\n```")

    @staticmethod
    async def repl_handle_syntaxerror(ctx, exc: SyntaxError):
        """Handles and points to syntax errors.

        We handle this differently from normal exceptions since we don't need a long traceback.
        """

        traceback_content = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, 0))
        await ctx.send(f"```py\n{traceback_content}\n```")

    @staticmethod
    async def attempt_add_reaction(msg: discord.Message, text: str):
        """Try to add a reaction, ignore if it fails"""
        try:
            await msg.add_reaction(text)
        except discord.HTTPException:
            pass

    @staticmethod
    def clean_sh_content(buffer: bytes):
        # decode the bytestring and strip any extra data we don't care for
        text = buffer.decode('utf8').replace('\r', '').strip('\n')
        # remove color-code characters and strip again for good measure
        return re.sub(r'\x1b[^m]*m', '', text).strip('\n')

    def sh_backend(self, code):
        """Open a subprocess, wait for it and format the output"""
        proc = subprocess.Popen(["/bin/bash", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = map(self.clean_sh_content, proc.communicate(timeout=30))

        # if this includes some stderr as well as stdout
        if err:
            out = out or '\u200b'
            total_length = len(out) + len(err)

            # if the whole thing won't fit in a message
            if total_length > 1968:
                # scale stdout and stderr to their proportions within a message
                out_resize_len = len(out) * (1960 / total_length)
                err_resize_len = len(err) * (1960 / total_length)

                # add ellipses to show these have been truncated
                # we show the last x amount of characters since they're usually the most important
                out = "...\n" + out[int(-out_resize_len):]
                err = "...\n" + err[int(-err_resize_len):]

            # format into codeblocks
            return f"```prolog\n{out}\n```\n```prolog\n{err}\n```"
        else:
            # if the stdout won't fit in a message
            if len(out) > 1980:
                out = "...\n" + out[-1980:]
            # format into a single codeblock
            return f"```prolog\n{out}\n```"

    @jsk.command(name="sh", aliases=["```sh"])
    async def sh_command(self, ctx: commands.Context, *, code: str):
        """Use the shell to run other CLI programs

        This supports invoking programs, but not other shell syntax.
        """

        code = utils.cleanup_codeblock(code)

        # create handle that'll add a right arrow reaction if this execution takes a long time
        handle = self.do_later(1, self.attempt_add_reaction, ctx.message, "\N{BLACK RIGHT-POINTING TRIANGLE}")
        try:
            result = await self.bot.loop.run_in_executor(None, self.sh_backend, code)
        except subprocess.TimeoutExpired:
            # the subprocess took more than 30 seconds to execute
            # this could be because it was busy or because it blocked waiting for input

            # cancel the arrow reaction handle
            handle.cancel()
            # add an alarm clock reaction
            await self.attempt_add_reaction(ctx.message, "\N{ALARM CLOCK}")
        except Exception as exc:
            # something went wrong trying to create the subprocess

            # cancel the arrow reaction handle
            handle.cancel()
            # add !! emote
            await self.attempt_add_reaction(ctx.message, "\N{DOUBLE EXCLAMATION MARK}")
            # handle this the same as a standard repl exception
            await self.repl_handle_exception(ctx, exc)
        else:
            # nothing went wrong

            # cancel the arrow reaction handle
            handle.cancel()
            # :tick:
            await self.attempt_add_reaction(ctx.message, "\N{WHITE HEAVY CHECK MARK}")
            # send the result of the command
            await ctx.send(result)

    @jsk.command(name="load")
    async def load_command(self, ctx: commands.Context, *args: str):
        # this list contains the info we'll output at the end
        formatting_list = []
        # the amount of exts trying to load that succeeded
        success_count = 0
        total_count = len(args)

        for ext_name in args:
            try:
                self.bot.load_extension(ext_name)
            except Exception as exc:
                # add the extension name, exception type and exception string truncated
                exception_text = str(exc)
                formatting_list.append(f"- {ext_name}\n! {exc.__class__.__name__}: {exception_text:.75}")
                continue
            else:
                formatting_list.append(f"+ {ext_name}")
                success_count += 1

        full_list = "\n\n".join(formatting_list)
        await ctx.send(f"{success_count}/{total_count} loaded successfully\n```diff\n{full_list}\n```")

    @jsk.command(name="unload")
    async def unload_command(self, ctx: commands.Context, *args: str):
        # this list contains the info we'll output at the end
        formatting_list = []
        # the amount of exts trying to unload that succeeded
        success_count = 0
        total_count = len(args)

        for ext_name in args:
            try:
                self.bot.unload_extension(ext_name)
            except Exception as exc:
                # add the extension name, exception type and exception string truncated
                exception_text = str(exc)
                formatting_list.append(f"- {ext_name}\n! {exc.__class__.__name__}: {exception_text:.75}")
                continue
            else:
                formatting_list.append(f"+ {ext_name}")
                success_count += 1

        full_list = "\n\n".join(formatting_list)
        await ctx.send(f"{success_count}/{total_count} unloaded successfully\n```diff\n{full_list}\n```")

    @jsk.command(name="reload")
    async def reload_command(self, ctx: commands.Context, *args: str):
        # this list contains the info we'll output at the end
        formatting_list = []
        # the amount of exts trying to reload that succeeded
        success_count = 0
        total_count = len(args)

        for ext_name in args:
            try:
                self.bot.unload_extension(ext_name)
                self.bot.load_extension(ext_name)
            except Exception as exc:
                # add the extension name, exception type and exception string truncated
                exception_text = str(exc)
                formatting_list.append(f"- {ext_name}\n! {exc.__class__.__name__}: {exception_text:.75}")
                continue
            else:
                formatting_list.append(f"+ {ext_name}")
                success_count += 1

        full_list = "\n\n".join(formatting_list)
        await ctx.send(f"{success_count}/{total_count} reloaded successfully\n```diff\n{full_list}\n```")
