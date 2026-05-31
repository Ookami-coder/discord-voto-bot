import discord
from discord.ext import commands, tasks
import asyncio
import math
import psycopg2
from datetime import datetime, timedelta
import os
import nest_asyncio

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

# CONTROL DE BOTONES PARA EXPULSIÓN, MUTEOS Y TRASLADOS MANUALES
class VotoControl(discord.ui.View):
    def __init__(self, miembro_objetivo, votantes_requeridos, accion, tiempo_minutos=None, canal_destino=None, mensaje_ctx=None):
        super().__init__(timeout=60.0)
        self.miembro_objetivo = miembro_objetivo
        self.votantes_requeridos = votantes_requeridos
        self.accion = accion
        self.tiempo_minutos = tiempo_minutos
        self.canal_destino = canal_destino
        self.mensaje_ctx = mensaje_ctx
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
            detalles = f" por {self.tiempo_minutos} minutos" if self.tiempo_minutos else ""
            if self.canal_destino: detalles = f" a {self.canal_destino.name}"
            
            await interaction.response.edit_message(content=f"🗳️ ¡Votación Aprobada! Aplicando **{self.accion}** a {self.miembro_objetivo.mention}{detalles}.", view=None)
            
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
            await interaction.response.edit_message(content=f"Votos para {self.accion} a {self.miembro_objetivo.mention}: {votos_actuales}/{self.votantes_requeridos}")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                await self.mensaje_ctx.edit(content=f"⏰ **Votación Cancelada:** Se acabó el tiempo de 1 minuto para decidir sobre {self.miembro_objetivo.mention}.", view=self)
            except Exception as e:
                print(f"Error al editar timeout manual: {e}")


# CONTROL DE BOTONES EXCLUSIVO PARA SOLICITUD DE INGRESO A CANAL LLENO
class VotoAccesoControl(discord.ui.View):
    def __init__(self, miembro_solicitante, canal_privado, votantes_requeridos, mensaje_ctx=None):
        super().__init__(timeout=60.0)
        self.miembro_solicitante = miembro_solicitante
        self.canal_privado = canal_privado
        self.votantes_requeridos = votantes_requeridos
        self.mensaje_ctx = mensaje_ctx
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
            await interaction.response.edit_message(content=f"🗳️ ¡Acceso Aprobado! Moviendo a {self.miembro_solicitante.mention} al canal {self.canal_privado.mention}.", view=None)
            
            if self.miembro_solicitante.voice:
                try:
                    await self.canal_privado.set_permissions(self.miembro_solicitante, connect=True)
                    await self.miembro_solicitante.move_to(self.canal_privado)
                except Exception as e:
                    print(f"Error al mover usuario permitido: {e}")
        else:
            await interaction.response.edit_message(content=f"Solicitud de entrada de {self.miembro_solicitante.mention}. Votos: {votos_actuales}/{self.votantes_requeridos}")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                await self.mensaje_ctx.edit(content=f"⏰ **Solicitud Rechazada:** Se agotó el tiempo de 1 minuto. No se permitió la entrada de {self.miembro_solicitante.mention} al canal {self.canal_privado.mention}.", view=self)
            except Exception as e:
                print(f"Error al editar timeout automático: {e}")


# COMANDOS MANUALES (!votar)
@bot.command()
async def votar(ctx, accion: str, miembro: discord.Member, argumento: str = None):
    if accion not in ["sacar", "mutear", "mover"]:
        await ctx.send("❌ Acción inválida. Usa `sacar`, `mutear` o `mover`.")
        return
    if not miembro.voice or not miembro.voice.channel:
        await ctx.send(f"❌ {miembro.display_name} no está en ningún canal de voz.")
        return

    tiempo = int(argumento) if accion == "mutear" and argumento and argumento.isdigit() else 5
    canal_destino = None

    if accion == "mover":
        if not argumento:
            await ctx.send("❌ Especifica el nombre o ID del canal de voz de destino.")
            return
        canal_destino = discord.utils.get(ctx.guild.voice_channels, name=argumento)
        if not canal_destino and argumento.isdigit():
            canal_destino = ctx.guild.get_channel(int(argumento))
        if not canal_destino:
            await ctx.send(f"❌ No encontré el canal de voz '{argumento}'.")
            return

    usuarios_canal = [m for m in miembro.voice.channel.members if not m.bot]
    votos_necesarios = math.ceil(len(usuarios_canal) / 2)

    view = VotoControl(miembro, votos_necesarios, accion, tiempo_minutos=tiempo, canal_destino=canal_destino)
    
    mensaje_texto = f"🗳️ **Votación Iniciada por {ctx.author.mention}:** ¿Desean **{accion}** a {miembro.mention}?"
    if accion == "mutear": 
        mensaje_texto += f" por {tiempo} minutos."
    if canal_destino: 
        mensaje_texto += f" al canal {canal_destino.mention}."
    mensaje_texto += f"\nSe necesitan **{votos_necesarios}** votos. ¡Tienen **1 minuto** para votar!"

    msg = await ctx.send(mensaje_texto, view=view)
    view.mensaje_ctx = msg

@bot.event
async def on_ready():
    print(f"🤖 Bot Online en la nube")
    try:
        verificar_muteos_expirados.start()
    except Exception:
        pass


# LÓGICA AUTOMÁTICA DETECTORA DE CANALES LLENOS Y CONTROL ANTI-MUTEADOS
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # 1. CONTROL ANTI-EVASIÓN DE MUTEOS
    if after.channel and not before.channel:
        conn = psycopg2.connect(DB_URI)
        cursor = conn.cursor()
        cursor.execute("SELECT expira_en FROM muteos WHERE usuario_id = %s AND servidor_id = %s", (member.id, member.guild.id))
        resultado = cursor.fetchone()
