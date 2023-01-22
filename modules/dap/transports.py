from __future__ import annotations
from typing import IO, Any, Callable

from ..import core
from .transport import Transport, TransportStderrOutputLog, TransportStdoutOutputLog

import socket
import os
import subprocess
import threading

class Process:
	processes: set[subprocess.Popen] = set()

	@staticmethod
	def cleanup_processes():
		for self in Process.processes:
			if self.poll() is not None:
				core.info('killing process')
				self.kill()

		Process.processes.clear()

	@staticmethod
	def remove_finished_processes():
		finished = []
		for self in Process.processes:
			if self.poll() is not None:
				finished.append(self)

		for f in finished:
			Process.processes.remove(f)

	@staticmethod
	def add_subprocess(process: subprocess.Popen):
		Process.remove_finished_processes()
		Process.processes.add(process)

	@staticmethod
	async def check_output(command: list[str], cwd: str|None = None) -> bytes:
		return await core.run_in_executor(lambda: subprocess.check_output(command, cwd=cwd))

	def __init__(self, command: list[str], cwd: str|None = None, env: dict[str, str]|None = None):
		# taken from Default/exec.py
		# Hide the console window on Windows
		startupinfo = None
		if os.name == "nt":
			startupinfo = subprocess.STARTUPINFO() #type: ignore
			startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW #type: ignore

		self.process = subprocess.Popen(command,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			stdin=subprocess.PIPE,
			shell=False,
			bufsize=0,
			startupinfo=startupinfo,
			cwd = cwd,
			env=env)

		Process.add_subprocess(self.process)

		stdin = self.process.stdin; assert stdin
		stderr = self.process.stderr; assert stderr
		stdout = self.process.stdout; assert stdout

		self.stdin = stdin
		self.stderr = stderr
		self.stdout = stdout

		self.closed = False
		

	def on_stdout(self, callback: Callable[[str], None]):
		thread = threading.Thread(target=self._read_all, args=(self.process.stdout, callback))
		thread.start()

	def on_stderr(self, callback: Callable[[str], None]):
		thread = threading.Thread(target=self._read_all, args=(self.process.stderr, callback))
		thread.start()

	def _read_all(self, file: Any, callback: Callable[[str], None]) -> None:
		while True:
			line = file.read(2**15).decode('UTF-8')
			if not line:
				break

			core.call_soon_threadsafe(callback, line)

	def _readline(self, pipe: IO[bytes]) -> bytes:
		if l := pipe.readline():
			return l
		raise EOFError

	def _read(self, pipe: IO[bytes], n: int) -> bytes:
		if l := pipe.read(n):
			return l
		raise EOFError

	async def readline(self, pipe: IO[bytes]) -> bytes:
		return await core.run_in_executor(lambda: self._readline(pipe))

	async def read(self, pipe: IO[bytes], nbytes: int) -> bytes:
		return await core.run_in_executor(lambda: self._read(pipe, nbytes))

	def dispose(self):
		self.closed = True
		try:
			self.process.kill()
		except Exception:
			core.exception()


class StdioTransport(Transport):
	def __init__(self, log: core.Logger, command: list[str], cwd: str|None = None, stderr: Callable[[str], None] | None = None):
		log.log('transport', f'-- stdio transport: {command}')
		
		def log_stderr(data: str):
			log.log('transport', TransportStderrOutputLog(data))
			if stderr:
				stderr(data)

		self.process = Process(command, cwd)
		self.process.on_stderr(log_stderr)

	def write(self, message: bytes) -> None:
		self.process.stdin.write(message)
		self.process.stdin.flush()

	def readline(self) -> bytes:
		if l := self.process.stdout.readline():
			return l
		raise EOFError

	def read(self, n: int) -> bytes:
		if l := self.process.stdout.read(n):
			return l
		raise EOFError

	def dispose(self) -> None:
		self.process.dispose()


class SocketTransport(Transport):
	def __init__(self, log: core.Logger, host: str, port: int):
		log.log('transport', f'-- socket transport: {host}:{port}')
		self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.socket.connect((host, port))
		self.stdin = self.socket.makefile('wb')
		self.stdout = self.socket.makefile('rb')
		self.process: Process|None = None

	@staticmethod
	async def connect_with_retry(log: core.Logger, host: str, port: int):
		exception: Exception|None = None
		for _ in range(0, 20):
			try:
				return SocketTransport(log, host, port)
			except Exception as e:
				await core.sleep(0.25)
				exception = e

		raise exception or core.Error('unreachable')

	@staticmethod
	async def connect_with_process(log: core.Logger, command: list[str], port: int, process_is_program_output: bool = False, cwd: str|None = None):
		log.log('transport', f'-- socket transport process: {command}')
		process = Process(command, cwd=cwd)

		if not process_is_program_output:
			process.on_stdout(lambda data: log.log('transport', TransportStdoutOutputLog(data)))
			process.on_stderr(lambda data: log.log('transport', TransportStderrOutputLog(data)))

		try:
			transport = await SocketTransport.connect_with_retry(log, 'localhost', port)
			transport.process = process
			return transport

		except Exception as e:
			process.dispose()
			raise e

	def write(self, message: bytes) -> None:
		self.stdin.write(message)
		self.stdin.flush()

	def readline(self) -> bytes:
		if l := self.stdout.readline():
			return l
		raise EOFError

	def read(self, n: int) -> bytes:
		if l := self.stdout.read(n):
			return l
		raise EOFError

	def dispose(self) -> None:
		try:
			self.socket.close()
		except:
			core.exception()

		if self.process:
			self.process.dispose()

