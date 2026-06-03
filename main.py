import discord
from discord.ext import commands, tasks
import asyncio
import math
import psycopg2
from datetime import datetime, timedelta, timezone
import os
import nest_asyncio
from fastapi import FastAPI
import uvicorn
from httpx import AsyncClient

# Permitir bucles anidados en entornos asincronos
nest_asyncio.apply()

# Configuracion de permisos de Discord
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_URI = os.getenv("DATABASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

app = FastAPI()

# --- FUNCIONES AUXILIARES DE BASE DE DATOS (THREAD-SAFE) ---
def ejecutar_query(query, params=(), fetch=False, commit=False):
    conn = psycopg2.connect(DB_URI)
    cursor = conn.cursor()
    cursor.execute(query, params)
    resultado = None
    if fetch:
        resultado = cursor.fetchall()
    if commit:
        conn.commit()
    cursor.close()
    conn.close()
    return resultado

def inicializar_db():
    query = """
        CREATE TABLE IF NOT EXISTS muteos (
            usuario_id BIGINT,
            servidor_id BIGINT,
            expira_en TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (usuario_id, servidor_id)
        )
    """
    ejecutar_query(query, commit=True)

# --- BUCLE DE VERIFICACION EN SEGUNDO PLANO ---
@tasks.loop(seconds=10)
async def verificar_muteos_expirados():
    try:
        ahora = datetime.now(timezone.utc)
        query_select = "SELECT usuario_id, servidor_id FROM muteos WHERE expira_en <= %s"
        expirados = await asyncio.to_thread(ejecutar_query, query_select, (ahora,), fetch=True)
        
        if expirados:
            for usuario_id, servidor_id in expirados:
                guild = bot.get_guild(servidor_id)
                if guild:
                    miembro = guild.get_member(usuario_id)
                    if miembro and miembro.voice:
                        try:
                            await miembro.edit(mute=False)
                            print(f"Desmuteado automaticamente por tiempo cumplido: {miembro.name}")
                        except Exception as e:
                            print(f"Error desmuteando a {usuario_id}: {e}")
                
                query_delete = "DELETE FROM muteos WHERE usuario_id = %s AND servidor_id = %s"
                await asyncio.to_thread(ejecutar_query, query_delete, (usuario_id, servidor_id), commit=True)
    except Exception as e:
        print(f"Error en bucle de verifacion de muteos: {e}")

# --- COMPONENTES DE INTERFAZ: VOTACION DE ACCESO MANUAL ---
class VotoAccesoControl(discord.ui.View):
    def __init__(self, miembro_solicitante, canal_privado, votantes_requeridos, mensaje_ctx=None):
        super().__init__(timeout=60.0)
        self.miembro_solicitante = miembro_solicitante
        self.canal_privado = canal_privado
        self.votantes_requeridos = votantes_requeridos
        self.mensaje_ctx = mensaje_ctx
        self.votos_favor = set()

    @discord.ui.button(label="Permitir Entrada", style=discord.ButtonStyle.success)
    async def permitir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice or interaction.user.voice.channel != self.canal_privado:
            await interaction.response.send_message("Solo los miembros dentro del canal privado pueden votar.", ephemeral=True)
            return

        if interaction.user.id in self.votos_favor:
            await interaction.response.send_message("Ya diste tu voto.", ephemeral=True)
            return

        self.votos_favor.add(interaction.user.id)
        votos_actuales = len(self.votos_favor)

        if votos_actuales >= self.votantes_requeridos:
            self.stop()
            await interaction.response.edit_message(content=f"Votacion Aprobada! Otorgando permisos y moviendo a {self.miembro_solicitante.mention} al canal {self.canal_privado.mention}.", view=None)
            
            try:
                await self.canal_privado.set_permissions(self.miembro_solicitante, connect=True)
                if self.miembro_solicitante.voice and self.miembro_solicitante.voice.channel:
                    await self.miembro_solicitante.move_to(self.canal_privado)
            except Exception as e:
                print(f"Error al mover u otorgar permisos al usuario permitido: {e}")
        else:
            await interaction.response.edit_message(content=f"Solicitud de entrada de {self.miembro_solicitante.mention}.\nVotos a favor: {votos_actuales}/{self.votantes_requeridos}")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                await self.mensaje_ctx.edit(content=f"Solicitud {self.miembro_solicitante.name} Rechazada por tiempo.", view=self)
            except Exception as e:
                print(f"Error en timeout de acceso: {e}")

# --- COMPONENTES DE INTERFAZ: CONTROL DE MODERACION (SACAR/MUTEAR) ---
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

    @discord.ui.button(label="Votar Si", style=discord.ButtonStyle.danger)
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
            
            await interaction.response.edit_message(content=f"Votacion Aprobada! Aplicando {self.accion} a {self.miembro_objetivo.mention}{detalles}.", view=None)
            
            if self.accion == "sacar":
                await self.miembro_objetivo.move_to(None)
            elif self.accion == "mover" and self.canal_destino:
                await self.miembro_objetivo.move_to(self.canal_destino)
            elif self.accion == "mutear" and self.tiempo_minutos:
                try:
                    await self.miembro_objetivo.edit(mute=True)
                except Exception as e:
                    print(f"Error al aplicar mute inicial por Discord: {e}")
                
                expiracion = datetime.now(timezone.utc) + timedelta(minutes=self.tiempo_minutos)
                query_mute = """
                    INSERT INTO muteos (usuario_id, servidor_id, expira_en) 
                    VALUES (%s, %s, %s) 
                    ON CONFLICT (usuario_id, servidor_id) 
                    DO UPDATE SET expira_en = EXCLUDED.expira_en
                """
                await asyncio.to_thread(ejecutar_query, query_mute, (self.miembro_objetivo.id, interaction.guild.id, expiracion), commit=True)
        else:
            await interaction.response.edit_message(content=f"Votos para {self.accion} a {self.miembro_objetivo.mention}: {votos_actuales}/{self.votantes_requeridos}")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.mensaje_ctx:
            try:
                await self.mensaje_ctx.edit(content=f"Votacion Cancelada para {self.miembro_objetivo.name}.", view=self)
            except Exception as e:
                print(f"Error al editar timeout manual: {e}")

# --- COMANDO DE SOLICITUD DE ACCESO A CANAL PRIVADO ---
@bot.command(name="solicitar")
async def solicitar_acceso(ctx, *, nombre_o_id_canal: str):
    canal = discord.utils.get(ctx.guild.voice_channels, name=nombre_o_id_canal)
    if not canal and nombre_o_id_canal.isdigit():
        canal = ctx.guild.get_channel(int(nombre_o_id_canal))

    if not canal or not isinstance(canal, discord.VoiceChannel):
        await ctx.send("No encontre un canal valido.")
        return

    usuarios_en_canal = [m for m in canal.members if not m.bot]
    if len(usuarios_en_canal) == 0:
        await ctx.send("El canal esta vacio.")
        return

    votos_necesarios = math.ceil(len(usuarios_en_canal) / 2)
    view = VotoAccesoControl(miembro_solicitante=ctx.author, canal_privado=canal, votantes_requeridos=votos_necesarios)
    
    mensaje_texto = (
        f"Solicitud de Entrada: {ctx.author.mention} quiere unirse a {canal.mention}.\n"
        f"Necesita {votos_necesarios} votos de la llamada. Tienen 1 minuto!"
    )
    msg = await ctx.send(mensaje_texto, view=view)
    view.mensaje_ctx = msg

# --- COMANDO PRINCIPAL DE VOTACION MODERADORA ---
@bot.command(name="voto")
async def iniciar_voto(ctx, accion: str, miembro: discord.Member, tiempo: int = None, *, argumento: str = None):
    accion_limpia = accion.lower()
    if accion_limpia not in ["sacar", "mover", "mutear"]:
        await ctx.send("Accion no valida. Usa: sacar, mover o mutear.")
        return

    if not miembro.voice or not miembro.voice.channel:
        await ctx.send("El usuario objetivo debe estar en canal de voz.")
        return

    canal_destino = None
    if accion_limpia == "mover":
        if not argumento: return await ctx.send("Especifica el nombre o ID del canal de voz de destino.")
        canal_destino = discord.utils.get(ctx.guild.voice_channels, name=argumento)
        if not canal_destino and argumento.isdigit():
            canal_destino = ctx.guild.get_channel(int(argumento))
        if not canal_destino:
            await ctx.send(f"No encontre el canal de voz '{argumento}'.")
            return

    if accion_limpia == "mutear" and not tiempo:
        await ctx.send("Especifica el tiempo en minutos para el muteo.")
        return

    usuarios_canal = [m for m in miembro.voice.channel.members if not m.bot]
    votos_necesarios = math.ceil(len(usuarios_canal) / 2)

    view = VotoControl(miembro, votos_necesarios, accion_limpia, tiempo_minutos=tiempo, canal_destino=canal_destino)
    
    mensaje_texto = f"Votacion Iniciada por {ctx.author.mention}: Desean {accion_limpia} a {miembro.mention}?"
    if accion_limpia == "mutear" and tiempo: 
        mensaje_texto += f" por {tiempo} minutos."
    if canal_destino: 
        mensaje_texto += f" al canal {canal_destino.mention}."
    mensaje_texto += f"\nSe necesitan {votos_necesarios} votos. Tienen 1 minuto para votar!"

    msg = await ctx.send(mensaje_texto, view=view)
    view.mensaje_ctx = msg

# --- EVENTOS DEL BOT ---
@bot.event
async def on_ready():
    print(f"Bot Online en la nube como {bot.user}")
    try:
        await asyncio.to_thread(inicializar_db)
        if not verificar_muteos_expirados.is_running():
            verificar_muteos_expirados.start()
        if not mantener_bot_vivo.is_running() and RENDER_EXTERNAL_URL:
            mantener_bot_vivo.start()
    except Exception as e:
        print(f"Error inicializando componentes en on_ready: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    if after.channel and not before.channel:
        query = "SELECT expira_en FROM muteos WHERE usuario_id = %s AND servidor_id = %s"
        resultado = await asyncio.to_thread(ejecutar_query, query, (member.id, member.guild.id), fetch=True)

        if resultado and len(resultado) > 0:
            fecha_expiracion = resultado
            ahora = datetime.now(timezone.utc)
            if fecha_expiracion > ahora:
                try:
                    await member.edit(mute=True)
                    print(f"Muteo re-aplicado automaticamente a {member.name} por evasion.")
                except Exception as e:
                    print(f"No se pudo re-mutear al usuario evasor: {e}")

# --- RUTAS DE FASTAPI (REQUERIDO POR RENDER) ---
@app.get("/")
async def root():
    return {"status": "online", "message": "Bot is running 24/7 with FastAPI"}

@app.on_event("startup")
async def startup_event():
    token = os.getenv("DISCORD_TOKEN")
    if token:
        asyncio.create_task(bot.start(token))
        print("Bot de Discord lanzado exitosamente en segundo plano asincrono.")
    else:
        print("Error critico: No se encontro la variable de entorno DISCORD_TOKEN.")

# --- SISTEMA ANTISHUTDOWN NATIVO (KEEP ALIVE ASINCRONO) ---
@tasks.loop(minutes=10)
async def mantener_bot_vivo():
    if not RENDER_EXTERNAL_URL:
        print("Advertencia: RENDER_EXTERNAL_URL no configurada. El bot podria dormirse.")
        return
    try:
        async with AsyncClient() as client:
            response = await client.get(RENDER_EXTERNAL_URL)
            if response.status_code == 200:
                print("Keep-Alive exitoso: Senal enviada correctamente a FastAPI.")
    except Exception as e:
        print(f"Error en el bucle de auto-ping: {e}")

# --- ARRANQUE PRINCIPAL DEL SERVIDOR WEB ---
if __name__ == "__main__":
    try:
        inicializar_db()
        print("Base de datos verificada e inicializada correctamente.")
    except Exception as e:
        print(f"Error inicializando la base de datos: {e}")

    puerto = int(os.getenv("PORT", 10000))
    print(f"Iniciando Uvicorn en el puerto {puerto}...")
    uvicorn.run("main.:app", host="0.0.0.0", port=puerto, reload=False)
