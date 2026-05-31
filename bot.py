# Forzando actualizacion de Render - Comandos Slash
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import math
import psycopg2
from datetime import datetime, timedelta
import os
import nest_asyncio
from fastapi import FastAPI
import uvicorn
from contextlib import asynccontextmanager

nest_asyncio.apply()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_URI = os.getenv("DATABASE_URL")

def inicializar_db():
    conn = psycopg2.connect(DB_URI)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS muteos (
            usuario_id BIGINT,
            servidor_id BIGINT,
            expira_en TEXT,
            PRIMARY KEY (usuario_id, servidor_id)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()

@tasks.loop(seconds=10)
async def verificar_muteos_expirados():
    try:
        conn = psycopg2.connect(DB_URI)
        cursor = conn.cursor()
        ahora = datetime.utcnow().isoformat()
        
        cursor.execute("SELECT usuario_id, servidor_id FROM muteos WHERE expira_en <= %s", (ahora,))
        expirados = cursor.fetchall()
        
        for usuario_id, servidor_id in expirados:
            guild = bot.get_guild(servidor_id)
            if guild:
                miembro = guild.get_member(usuario_id)
                if miembro and miembro.voice:
                    try:
                        await miembro.edit(mute=False)
                    except Exception as e:
                        print(f"Error desmuteando: {e}")
            
            cursor.execute("DELETE FROM muteos WHERE usuario_id = %s AND servidor_id = %s", (usuario_id, servidor_id))
        
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error en base de datos: {e}")

# INTERFAZ VISUAL: BOTONES PARA MODERACIÓN MANUAL
class VotoControl(discord.ui.View):
    def __init__(self, miembro_objetivo, votantes_requeridos, accion, tiempo_minutos=None, canal_destino=None, mensaje_ctx=None, embed_original=None):
        super().__init__(timeout=60.0)
        self.miembro_objetivo = miembro_objetivo
        self.votantes_requeridos = votantes_requeridos
        self.accion = accion
        self.tiempo_minutos = tiempo_minutos
        self.canal_destino = canal_destino
        self.mensaje_ctx = mensaje_ctx
        self.embed_original = embed_original
        self.votos_favor = set()

    @discord.ui.button(label="Votar SÍ", style=discord.ButtonStyle.danger, emoji="✅")
    async def votar_si(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice or interaction.user.voice.channel != self.miembro_objetivo.voice.channel:
            await interaction.response.send_message("Debes estar en la misma llamada para votar.", ephemeral=True)
            return

        if interaction.user.id in self.votos_favor:
            await interaction.response.send_message("Ya has votado.", ephemeral=True)
            return

        self.votos_favor.add(interaction.user.id)
        votos_actuales = len(self.votos_favor)

        if votos_actuales >= self.votantes_requeridos:
            self.stop()
            
            embed = discord.Embed(
                title="🗳️ ¡Votación Aprobada!",
                description=f"La comunidad ha decidido aplicar **{self.accion}** a {self.miembro_objetivo.mention}.",
                color=discord.Color.green()
            )
            if self.tiempo_minutos: embed.add_field(name="Duración", value=f"{self.tiempo_minutos} minutos")
            if self.canal_destino: embed.add_field(name="Destino", value=self.canal_destino.mention)
            
            await interaction.response.edit_message(embed=embed, view=None)
            
            if self.accion == "sacar":
                await self.miembro_objetivo.move_to(None)
            elif self.accion == "mover" and self.canal_destino:
                await self.miembro_objetivo.move_to(self.canal_destino)
            elif self.accion == "mutear" and self.tiempo_minutos:
                await self.miembro_objetivo.edit(mute=True)
                
                conn = psycopg2.connect(DB_URI)
                cursor = conn.cursor()
                expiracion = (datetime.utcnow() + timedelta(minutes=self.tiempo_minutos)).isoformat()
                cursor.execute("""
                    INSERT INTO muteos (usuario_id, servidor_id, expira_en) 
                    VALUES (%s, %s, %s) 
                    ON CONFLICT (usuario_id, servidor_id) 
                    DO UPDATE SET expira_en = EXCLUDED.expira_en
                """, (self.miembro_objetivo.id, interaction.guild.id, expiracion))
                conn.commit()
                cursor.close()
                conn.close()
        else:
            nuevo_embed = self.embed_original.copy()
            nuevo_embed.set_footer(text=f"Progreso: {votos_actuales}/{self.votantes_requeridos} votos requeridos")
            await interaction.response.edit_message(embed=nuevo_embed)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                embed = discord.Embed(
                    title="⏰ Votación Cancelada",
                    description=f"Se acabó el tiempo de 1 minuto para decidir sobre {self.miembro_objetivo.mention}.",
                    color=discord.Color.orange()
                )
                await self.mensaje_ctx.edit(embed=embed, view=self)
            except Exception as e:
                print(f"Error al editar timeout manual: {e}")


# INTERFAZ VISUAL: BOTONES PARA ACCESO A CANALES LLENOS
class VotoAccesoControl(discord.ui.View):
    def __init__(self, miembro_solicitante, canal_privado, votantes_requeridos, mensaje_ctx=None, embed_original=None):
        super().__init__(timeout=60.0)
        self.miembro_solicitante = miembro_solicitante
        self.canal_privado = canal_privado
        self.votantes_requeridos = votantes_requeridos
        self.mensaje_ctx = mensaje_ctx
        self.embed_original = embed_original
        self.votos_favor = set()

    @discord.ui.button(label="Permitir Entrada", style=discord.ButtonStyle.success, emoji="🔓")
    async def permitir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice or interaction.user.voice.channel != self.canal_privado:
            await interaction.response.send_message("Solo los miembros dentro del canal lleno pueden votar.", ephemeral=True)
            return

        if interaction.user.id in self.votos_favor:
            await interaction.response.send_message("Ya diste tu voto.", ephemeral=True)
            return

        self.votos_favor.add(interaction.user.id)
        votos_actuales = len(self.votos_favor)

        if votos_actuales >= self.votantes_requeridos:
            self.stop()
            
            embed = discord.Embed(
                title="🗳️ ¡Acceso Aprobado!",
                description=f"Se ha permitido la entrada de {self.miembro_solicitante.mention} al canal {self.canal_privado.mention}.",
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            
            if self.miembro_solicitante.voice:
                try:
                    await self.canal_privado.set_permissions(self.miembro_solicitante, connect=True)
                    await self.miembro_solicitante.move_to(self.canal_privado)
                except Exception as e:
                    print(f"Error al mover usuario permitido: {e}")
        else:
            nuevo_embed = self.embed_original.copy()
            nuevo_embed.set_footer(text=f"Progreso: {votos_actuales}/{self.votantes_requeridos} votos requeridos")
            await interaction.response.edit_message(embed=nuevo_embed)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                embed = discord.Embed(
                    title="⏰ Solicitud Rechazada",
                    description=f"Se agotó el tiempo de 1 minuto. No se permitió la entrada de {self.miembro_solicitante.mention} al canal {self.canal_privado.mention}.",
                    color=discord.Color.red()
                )
                await self.mensaje_ctx.edit(embed=embed, view=self)
            except Exception as e:
                print(f"Error al editar timeout automático: {e}")


# COMANDO DE BARRA DIAGONAL (/votar)
@bot.tree.command(name="votar", description="Inicia una votación democrática para moderar la llamada de voz.")
@app_commands.describe(
    accion="Selecciona qué quieres hacer",
    miembro="El usuario al que se le aplicará la acción",
    argumento="Minutos para mutear O Nombre/ID del canal de voz para mover"
)
@app_commands.choices(accion=[
    app_commands.Choice(name="Sacar de la llamada", value="sacar"),
    app_commands.Choice(name="Mutear temporalmente", value="mutear"),
    app_commands.Choice(name="Mover a otra sala", value="mover")
])
async def votar_slash(interaction: discord.Interaction, accion: str, miembro: discord.Member, argumento: str = None):
    if not miembro.voice or not miembro.voice.channel:
        await interaction.response.send_message(f"❌ {miembro.display_name} no está en ningún canal de voz.", ephemeral=True)
        return

    tiempo = int(argumento) if accion == "mutear" and argumento and argumento.isdigit() else 5
    canal_destino = None

    if accion == "mover":
        if not argumento:
            await interaction.response.send_message("❌ Especifica el nombre o ID del canal de voz de destino en el campo de argumento.", ephemeral=True)
            return

# ====================================================================
# Servidor web falso para engañar a Render y evitar el Port Timeout
from threading import Thread
from http.server import SimpleHTTPRequestHandler, HTTPServer

def run_fake_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    server.serve_forever()

Thread(target=run_fake_server, daemon=True).start()
# ====================================================================

# TU LÍNEA FINAL ORIGINAL (Mantenla tal cual):
bot.run(os.getenv("DISCORD_TOKEN"))
