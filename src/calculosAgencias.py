"""Calculadora de Agencias.

CLI interactivo que replica los cálculos de comisión, fee, conversión de puntos,
DropOff y MarkUp de los marketplaces de viajes para verificar en QA lo que
cobra la agencia frente a lo que debería cobrar Sabre/el proveedor.

NOTA: las fórmulas vienen de la primera versión del programa; MarkUp (opción 6) no estaba en ella
y es la única sin referencia.
"""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import pyperclip
import requests
from colorama import Fore, Style, init as colorama_init

RUTA_MARKETPLACES = Path(__file__).resolve().parent / 'marketplaces.json'

ELLIPSES = 26
CODIGO = Fore.YELLOW + Style.BRIGHT  # todo lo que el usuario puede teclear
MAX_INTENTOS = 3

# Marketplaces cuyo Sabre responde en otra moneda, solo en Vuelos TA (opción 4).
MONEDA_SABRE_VUELOS: dict[str, str] = {'kon-cr': 'CRC'}

MODOS_PAGO = {'pp': 'puntos_plata', 'sp': 'solo_puntos', 'sl': 'solo_plata'}


# ═══════════════════════════════════════════════════════════════════════
# Modelo
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Agencia:
    # Campos nuevos SIEMPRE al final: agregar_marketplace() llama posicional.
    nombre: str
    punto: float = 0.0
    ta_f: float = 0.0
    ta_p: float = 1.0
    fee_f: float = 0.0
    fee_p: float = 1.0
    currency: str = 'USD'
    agencia_id: str = ''
    url_mkp: str = ''
    url_admin: str = ''
    usuario: str = ''
    password: str = ''
    modo_pago: str = 'puntos_plata'
    markup_fijo: float = 0.0
    markup_porcentual: float = 1.0


@dataclass
class Familia:
    nombre: str
    culturas: list[str] = field(default_factory=list)


@dataclass
class Sesion:
    codigo_actual: str = ''
    agencia: Agencia | None = None
    cambios_pendientes: bool = False
    ultimo_resultado: tuple[str, str] | None = None  # (de qué cálculo, valor), para `c`


def marcar_cambio(sesion: Sesion) -> None:
    sesion.cambios_pendientes = True


# código -> (atributo, etiqueta, tipo). El orden es el que ve el humano en `edt`.
CAMPOS_AGENCIA = {
    'n':   ('nombre', 'Nombre', 'texto'),
    'cur': ('currency', 'Moneda', 'texto_upper'),
    'mp':  ('modo_pago', 'Modo de pago', 'modo_pago'),
    'p':   ('punto', 'Valor del punto', 'float'),
    'tf':  ('ta_f', 'TA fija', 'float'),
    'tp':  ('ta_p', 'TA %', 'pct'),
    'ff':  ('fee_f', 'Fee fijo', 'float'),
    'fp':  ('fee_p', 'Fee %', 'pct'),
    'mkf': ('markup_fijo', 'MarkUp fijo', 'float'),
    'mkp': ('markup_porcentual', 'MarkUp %', 'pct'),
    'id':  ('agencia_id', 'Agencia ID', 'texto'),
    'um':  ('url_mkp', 'URL marketplace', 'texto'),
    'ua':  ('url_admin', 'URL admin', 'texto'),
    'us':  ('usuario', 'Usuario', 'texto'),
    'pw':  ('password', 'Password', 'texto'),
}

# Editables desde la revisión rápida antes de un cálculo.
CAMPOS_EDITABLES = {c: CAMPOS_AGENCIA[c] for c in ('p', 'tf', 'tp', 'ff', 'fp', 'mkf', 'mkp')}

# Datos de referencia que solo se consultan a demanda (y se copian al portapapeles).
CAMPOS_EXTRA = {c: CAMPOS_AGENCIA[c] for c in ('id', 'um', 'ua', 'us', 'pw')}

CAMPOS_CALCULO = ('punto', 'ta_f', 'ta_p', 'fee_f', 'fee_p', 'modo_pago', 'markup_fijo', 'markup_porcentual')


# ═══════════════════════════════════════════════════════════════════════
# I/O de marketplaces (familias con culturas)
# ═══════════════════════════════════════════════════════════════════════

def nombre_derivado(nombre_familia: str, sufijo: str) -> str:
    return f'{nombre_familia} {sufijo.upper()}'


def cargar_marketplaces(ruta: Path) -> tuple[dict[str, Agencia], dict[str, Familia]]:
    with open(ruta, encoding='utf-8') as f:
        data = json.load(f)
    marketplaces: dict[str, Agencia] = {}
    familias: dict[str, Familia] = {}
    for codigo, entrada in data.items():
        if 'culturas' not in entrada:
            marketplaces[codigo] = Agencia(**entrada)
            continue
        base = {k: v for k, v in entrada.items() if k not in ('culturas', 'nombre')}
        familias[codigo] = Familia(entrada['nombre'], list(entrada['culturas']))
        for sufijo, propios in entrada['culturas'].items():
            datos = {**base, **propios}
            datos.setdefault('nombre', nombre_derivado(entrada['nombre'], sufijo))
            marketplaces[f'{codigo}-{sufijo}'] = Agencia(**datos)
    return marketplaces, familias


def familia_de(codigo: str, familias: dict[str, Familia]) -> str:
    """Código de la familia a la que pertenece `codigo`, o '' si es plano."""
    if '-' not in codigo:
        return ''
    fam, sufijo = codigo.rsplit('-', 1)
    return fam if fam in familias and sufijo in familias[fam].culturas else ''


def bloque_familia(codigo_familia: str, familia: Familia, marketplaces: dict[str, Agencia]) -> dict:
    """Colapsa las culturas de una familia: compartido = idéntico en TODAS."""
    agencias = [(s, asdict(marketplaces[f'{codigo_familia}-{s}'])) for s in familia.culturas]
    campos = [f.name for f in fields(Agencia) if f.name != 'nombre']
    primera = agencias[0][1]
    compartidos = [c for c in campos if all(a[c] == primera[c] for _, a in agencias)]

    bloque: dict = {'nombre': familia.nombre}
    for c in compartidos:
        bloque[c] = primera[c]
    culturas = {}
    for sufijo, a in agencias:
        propios = {}
        if a['nombre'] != nombre_derivado(familia.nombre, sufijo):
            propios['nombre'] = a['nombre']
        for c in campos:
            if c not in compartidos:
                propios[c] = a[c]
        culturas[sufijo] = propios
    bloque['culturas'] = culturas
    return bloque


def guardar_marketplaces(ruta: Path, marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> None:
    salida: dict = {}
    for codigo, agencia in marketplaces.items():
        fam = familia_de(codigo, familias)
        if not fam:
            salida[codigo] = asdict(agencia)
        elif fam not in salida:
            salida[fam] = bloque_familia(fam, familias[fam], marketplaces)
    # Escritura atómica: un fallo a mitad no deja el catálogo corrupto.
    ruta = Path(ruta)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, ruta)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ═══════════════════════════════════════════════════════════════════════
# Resolución de códigos
# ═══════════════════════════════════════════════════════════════════════

def resolver_agencia(entrada: str, marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> str:
    """Código exacto (agencia o familia) o forma concatenada tolerada (`koncr` -> `kon-cr`)."""
    e = entrada.strip().lower()
    if e in marketplaces or e in familias:
        return e
    for fam, familia in familias.items():
        for sufijo in familia.culturas:
            if e == fam + sufijo:
                return f'{fam}-{sufijo}'
    return ''


def resolver_codigo(entrada: str, marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> tuple[str, str]:
    """(codigo, campo_extra). Primero la entrada completa como agencia; luego `<agencia>-<campo>`."""
    codigo = resolver_agencia(entrada, marketplaces, familias)
    if codigo:
        return codigo, ''
    e = entrada.strip().lower()
    if '-' in e:
        izquierda, campo = e.rsplit('-', 1)
        if campo in CAMPOS_EXTRA:
            codigo = resolver_agencia(izquierda, marketplaces, familias)
            if codigo:
                return codigo, campo
    return '', ''


# ═══════════════════════════════════════════════════════════════════════
# Entradas
# ═══════════════════════════════════════════════════════════════════════

class Cancelado(Exception):
    pass


def error(msg: str) -> None:
    print(Fore.RED + msg + Style.RESET_ALL)


def aviso(msg: str) -> None:
    print(Fore.YELLOW + msg + Style.RESET_ALL)


def resultado(msg: str) -> None:
    print(Fore.GREEN + msg + Style.RESET_ALL)


def leer(mensaje: str) -> str:
    texto = input(mensaje).strip()
    if texto.lower() == 'x':
        raise Cancelado
    return texto


class NumeroAmbiguo(ValueError):
    pass


def a_numero(texto: str, miles: bool = True) -> float:
    """Acepta `10000.5`, `10000,5`, `10.000,5` y `10.000` (miles, igual que se muestra en pantalla).

    Con `miles=False` (tasas y valores de configuración, donde tres decimales son normales) un
    `17.857` no se adivina: se rechaza como ambiguo.
    """
    t = texto.replace(' ', '')
    if re.fullmatch(r'-?[1-9][0-9]{0,2}\.[0-9]{3}', t) and not miles:
        raise NumeroAmbiguo(f"'{t}' es ambiguo: escribe {t.replace('.', '')} (miles) o {t.replace('.', ',')} (decimal).")
    if re.fullmatch(r'-?[1-9][0-9]{0,2}(\.[0-9]{3})+(,[0-9]+)?', t):
        t = t.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'-?[0-9]+[.,]?[0-9]*|-?[.,][0-9]+', t):
        t = t.replace(',', '.')
    else:
        raise ValueError(texto)
    return float(t)


def a_monto(texto: str) -> float:
    valor = a_numero(texto)
    if valor < 0:
        raise ValueError(texto)
    return valor


def _pedir(mensaje: str, convertir, por_defecto=None, opcional: bool = False, msg_error: str = 'Valor inválido.'):
    for _ in range(MAX_INTENTOS):
        texto = leer(mensaje)
        if texto == '' and opcional:
            return por_defecto
        try:
            return convertir(texto)
        except ValueError as e:
            error(f'{e if isinstance(e, NumeroAmbiguo) else msg_error} (x para cancelar)')
    error(f'{MAX_INTENTOS} intentos inválidos: cancelado.')
    raise Cancelado


def pedir_float(mensaje: str) -> float:
    return _pedir(mensaje, a_monto, msg_error='Escribe un número positivo.')


def pedir_int(mensaje: str) -> int:
    def convertir(t):
        valor = a_monto(t)
        if valor != int(valor):
            raise ValueError
        return int(valor)
    return _pedir(mensaje, convertir, msg_error='Escribe un número entero positivo.')


def pedir_float_opcional(mensaje: str, por_defecto=None):
    return _pedir(mensaje, lambda t: a_numero(t, miles=False), por_defecto, opcional=True,
                  msg_error='Escribe un número o Enter.')


def pedir_moneda(mensaje: str, por_defecto: str) -> str:
    def convertir(t):
        if not re.fullmatch(r'[A-Za-z]{3}', t):
            raise ValueError
        return t.upper()
    return _pedir(mensaje, convertir, por_defecto, opcional=True,
                  msg_error=f'Código ISO de 3 letras (Enter = {por_defecto}).')


def pedir_texto(mensaje: str, por_defecto: str = '') -> str:
    texto = leer(mensaje)
    return texto if texto else por_defecto


def pedir_modo_pago(actual: str) -> str:
    def convertir(t):
        if t.lower() not in MODOS_PAGO:
            raise ValueError
        return MODOS_PAGO[t.lower()]
    return _pedir(f'Modo de pago pp/sp/sl [{actual}]: ', convertir, actual, opcional=True,
                  msg_error='Usa pp (puntos+plata), sp (solo puntos) o sl (solo plata).')


def valor_porcentual_desde_input(valor: float) -> float:
    """10 (lo que digita el usuario) -> 1.10 (factor guardado)."""
    return 1 + valor / 100


def pct(factor: float) -> str:
    return f'{round((factor - 1) * 100, 6):g}%'


def confirmar(mensaje: str) -> bool:
    return input(mensaje).strip().lower() == 's'


# ═══════════════════════════════════════════════════════════════════════
# Presentación
# ═══════════════════════════════════════════════════════════════════════

def formatear_numero(valor: float, decimales: int = 2) -> str:
    """Solo para mostrar: 10000.5 -> '10.000,5'. Nunca dentro de un cálculo."""
    texto = f'{valor:,.{decimales}f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    if ',' in texto:
        texto = texto.rstrip('0').rstrip(',')
    return texto


def linea_menu(texto: str, codigo: str) -> str:
    if len(texto) > ELLIPSES:
        texto = texto[:ELLIPSES - 3] + '...'
    puntos = '.' * (ELLIPSES - len(texto))
    return f'  {texto} {Style.DIM}{puntos}{Style.RESET_ALL}: {CODIGO}{codigo}{Style.RESET_ALL}'


def monto(valor: float, moneda: str, decimales: int = 2) -> str:
    return (f'{Style.BRIGHT}{formatear_numero(valor, decimales)}{Style.RESET_ALL} '
            f'{Fore.CYAN}{moneda}{Style.RESET_ALL}')


def separador(caracter: str = '─') -> None:
    print(Fore.CYAN + caracter * 40 + Style.RESET_ALL)


def mostrar_bloque(sesion: Sesion, titulo: str, filas: list[tuple[str, float, str]],
                   nota: str = '', color: str = Fore.GREEN) -> None:
    """Recuadro con las cifras alineadas por la coma decimal. La última fila es el valor final (`c`)."""
    numeros = [formatear_numero(valor) for _, valor, _ in filas]
    partes = [numero.partition(',') for numero in numeros]
    ancho_ent = max(len(entero) for entero, _, _ in partes)
    ancho_dec = max(len(coma + dec) for _, coma, dec in partes)
    ancho_etq = max(len(etiqueta) for etiqueta, _, _ in filas)
    ancho_uni = max(len(unidad) for _, _, unidad in filas)
    # El ancho se mide sobre texto sin color: los códigos ANSI no ocupan columnas.
    ancho = max(ancho_etq + 2 + ancho_ent + ancho_dec + 1 + ancho_uni, len(nota), len(titulo) + 1)
    borde = lambda texto: f'{Fore.CYAN}{texto}{Style.RESET_ALL}'

    print(borde('┌─ ') + Style.BRIGHT + titulo + Style.RESET_ALL + borde(' ' + '─' * (ancho - len(titulo) - 1) + '┐'))
    for i, ((etiqueta, _, unidad), (entero, coma, dec)) in enumerate(zip(filas, partes)):
        numero = entero.rjust(ancho_ent) + (coma + dec).ljust(ancho_dec)
        tono = color if i == len(filas) - 1 else ''
        relleno = ' ' * (ancho - (ancho_etq + 2 + len(numero) + 1 + len(unidad)))
        print(f'{borde("│")} {etiqueta.ljust(ancho_etq)}  {Style.BRIGHT}{tono}{numero}{Style.RESET_ALL} '
              f'{Fore.CYAN}{unidad}{Style.RESET_ALL}{relleno} {borde("│")}')
    if nota:
        print(f'{borde("│")} {color}{nota.ljust(ancho)}{Style.RESET_ALL} {borde("│")}')
    print(borde('└' + '─' * (ancho + 2) + '┘'))

    sesion.ultimo_resultado = (f'{titulo} · {sesion.codigo_actual.upper()} · {filas[-1][0]}',
                               f'{numeros[-1]} {filas[-1][2]}')


def copiar_al_portapapeles(texto: str) -> bool:
    try:
        pyperclip.copy(texto)
        return True
    except pyperclip.PyperclipException:
        return False


def copiar_ultimo_resultado(sesion: Sesion) -> None:
    if sesion.ultimo_resultado is None:
        error('Todavía no hay ningún resultado para copiar.')
        return
    origen, valor = sesion.ultimo_resultado
    if copiar_al_portapapeles(valor):
        resultado(f'Copiado: {valor}  ({origen})')
    else:
        error(f'No se pudo copiar al portapapeles: {valor}  ({origen})')


puntos_mas_plata_en_plata       = 'Puntos + Plata en Plata'
puntos_mas_plata_en_puntos      = 'Puntos + Plata en Puntos'
promo_code_txt                  = 'PromoCode P+P'
vuelos_ta_txt                   = 'Vuelos TA + FEE'
dropoff_txt                     = 'Autos con DropOff'
markup_txt                      = 'MarkUp'
puntos_mas_plata_a_otra_moneda  = 'Convertir Puntos + Plata'
listar_txt                      = 'Agencias'
editar_txt                      = 'Editar Agencia'
nuevo_txt                       = 'Nuevo Marketplace'
eliminar_txt                    = 'Eliminar Marketplace'
menu_txt                        = 'Mostrar Menú'
copiar_txt                      = 'Copiar Resultado'
limpiar_txt                     = 'Limpiar Pantalla'
salir_txt                       = 'Salir'

COMANDOS = {'0', '1', '2', '3', '4', '5', '6', '7', 'c', 'edt', 'new', 'del', 'm', 'cls', 'jdra', 's'}
RESERVADAS = COMANDOS | set(CAMPOS_EXTRA)


def menu() -> None:
    separador('═')
    print(f'  {Fore.CYAN}{Style.BRIGHT}CALCULADORA DE AGENCIAS{Style.RESET_ALL}')
    separador()
    for texto, codigo in (
        (listar_txt, '0'),
        (puntos_mas_plata_en_plata, '1'), (puntos_mas_plata_en_puntos, '2'), (promo_code_txt, '3'),
        (vuelos_ta_txt, '4'), (dropoff_txt, '5'), (markup_txt, '6'),
        (puntos_mas_plata_a_otra_moneda, '7'),
    ):
        print(linea_menu(texto, codigo))
    separador()
    for texto, codigo in ((copiar_txt, 'c'), (menu_txt, 'm'), (limpiar_txt, 'cls'), (salir_txt, 's')):
        print(linea_menu(texto, codigo))
    separador('═')


def menu_agencias(marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> None:
    """Submenú `0`: lista de agencias, su gestión y la ayuda para cargarlas."""
    ejemplo = lambda codigo: f'{CODIGO}{codigo}{Style.RESET_ALL}'
    separador('═')
    print(f'  {Fore.CYAN}{Style.BRIGHT}{listar_txt.upper()}{Style.RESET_ALL}')
    separador()
    listar_marketplaces(marketplaces, familias)
    separador()
    for texto, codigo in ((editar_txt, 'edt'), (nuevo_txt, 'new'), (eliminar_txt, 'del')):
        print(linea_menu(texto, codigo))
    separador()
    # Sin `<...>-<...>`: algunas fuentes de consola juntan esos símbolos en una flecha.
    print(f'  Para cargar una agencia: {ejemplo("cap")}, {ejemplo("kon-gt")}')
    print(f'  Para copiar un dato: {ejemplo("cap-id")}, {ejemplo("kon-gt-pw")}')
    print(f'  Para cancelar cualquier pregunta: {ejemplo("x")}')
    separador('═')


def listar_marketplaces(marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> None:
    vistas = set()
    for codigo, agencia in marketplaces.items():
        fam = familia_de(codigo, familias)
        if fam:
            if fam not in vistas:
                vistas.add(fam)
                print(linea_menu(familias[fam].nombre, f'{fam} ({"/".join(familias[fam].culturas)})'))
        else:
            print(linea_menu(agencia.nombre, codigo))


def mostrar_ficha(agencia: Agencia) -> None:
    moneda = agencia.currency
    separador()
    print(f'  {Fore.CYAN}{Style.BRIGHT}{agencia.nombre}{Style.RESET_ALL}  ·  '
          f'{Fore.CYAN}{moneda}{Style.RESET_ALL}  ·  {agencia.modo_pago}')
    print(f'  Punto:  {monto(agencia.punto, moneda, 4)}')
    print(f'  TA:     fija {monto(agencia.ta_f, moneda)} / {Style.BRIGHT}{pct(agencia.ta_p)}{Style.RESET_ALL}')
    print(f'  Fee:    fijo {monto(agencia.fee_f, moneda)} + {Style.BRIGHT}{pct(agencia.fee_p)}{Style.RESET_ALL}')
    print(f'  MarkUp: fijo {monto(agencia.markup_fijo, moneda)} + '
          f'{Style.BRIGHT}{pct(agencia.markup_porcentual)}{Style.RESET_ALL}')
    separador()
    print('  Ver más (se copia al portapapeles): '
          + '  ·  '.join(f'{etiqueta} = {CODIGO}{c}{Style.RESET_ALL}' for c, (_, etiqueta, _) in CAMPOS_EXTRA.items()))


def mostrar_dato_extra(agencia: Agencia, campo: str) -> None:
    atributo, etiqueta, _ = CAMPOS_EXTRA[campo]
    valor = getattr(agencia, atributo)
    copiado = copiar_al_portapapeles(valor)
    if campo == 'pw':
        if copiado:
            resultado(f'{etiqueta} de {agencia.nombre} copiado al portapapeles.')
        else:
            error('No se pudo copiar al portapapeles (el password no se muestra en pantalla).')
        return
    resultado(f'{etiqueta}: {valor}' + ('  (copiado)' if copiado else '  (no se pudo copiar)'))


# ═══════════════════════════════════════════════════════════════════════
# Edición de agencias
# ═══════════════════════════════════════════════════════════════════════

def editar_campo(agencia: Agencia, atributo: str, etiqueta: str, tipo: str) -> bool:
    actual = getattr(agencia, atributo)
    if tipo == 'float':
        nuevo = pedir_float_opcional(f'{etiqueta} [{formatear_numero(actual, 4)}]: ', actual)
    elif tipo == 'pct':
        valor = pedir_float_opcional(f'{etiqueta} (ej. 10 = 10%) [{pct(actual)}]: ', None)
        nuevo = actual if valor is None else valor_porcentual_desde_input(valor)
    elif tipo == 'texto_upper':
        nuevo = pedir_moneda(f'{etiqueta} [{actual}]: ', actual)
    elif tipo == 'modo_pago':
        nuevo = pedir_modo_pago(actual)
    elif atributo == 'password':
        nuevo = pedir_texto(f'{etiqueta} (Enter = sin cambio): ', actual)
    else:
        nuevo = pedir_texto(f'{etiqueta} [{actual}]: ', actual)
    if nuevo == actual:
        return False
    setattr(agencia, atributo, nuevo)
    return True


def editar_agencia(sesion: Sesion) -> None:
    agencia = sesion.agencia
    while True:
        for codigo, (atributo, etiqueta, tipo) in CAMPOS_AGENCIA.items():
            valor = getattr(agencia, atributo)
            if atributo == 'password':
                valor = '****' if valor else ''
            elif tipo == 'pct':
                valor = pct(valor)
            print(linea_menu(etiqueta, codigo) + f'  {valor}')
        codigo = leer('Campo a editar (Enter = terminar): ').lower()
        if not codigo:
            return
        if codigo not in CAMPOS_AGENCIA:
            error('Código de campo desconocido.')
            continue
        if editar_campo(agencia, *CAMPOS_AGENCIA[codigo]):
            marcar_cambio(sesion)
            avisar_ta_mal_cargada(agencia)


def revision_rapida(sesion: Sesion, codigos: list[str]) -> None:
    """Muestra los valores que usa el cálculo y deja corregirlos antes de seguir."""
    agencia = sesion.agencia
    while True:
        partes = []
        for codigo in codigos:
            atributo, etiqueta, tipo = CAMPOS_EDITABLES[codigo]
            valor = getattr(agencia, atributo)
            partes.append(f'{etiqueta} [{codigo}]: {pct(valor) if tipo == "pct" else formatear_numero(valor, 4)}')
        print('  ' + '  ·  '.join(partes))
        codigo = leer('Enter para continuar o código para editar: ').lower()
        if not codigo:
            return
        if codigo not in codigos:
            error('Ese campo no aplica a este cálculo.')
            continue
        if editar_campo(agencia, *CAMPOS_EDITABLES[codigo]):
            marcar_cambio(sesion)


def pedir_bloque_calculo() -> tuple[float, float, float, float, float, float]:
    ta_f = pedir_float_opcional('TA fija [0]: ', 0.0)
    ta_p = 1.0
    if ta_f == 0:  # La TA es fija O porcentual, nunca ambas.
        ta_p = valor_porcentual_desde_input(pedir_float_opcional('TA % [0]: ', 0.0))
    fee_f = pedir_float_opcional('Fee fijo [0]: ', 0.0)
    fee_p = valor_porcentual_desde_input(pedir_float_opcional('Fee % [0]: ', 0.0))
    markup_fijo = pedir_float_opcional('MarkUp fijo [0]: ', 0.0)
    markup_p = valor_porcentual_desde_input(pedir_float_opcional('MarkUp % [0]: ', 0.0))
    return ta_f, ta_p, fee_f, fee_p, markup_fijo, markup_p


def agregar_marketplace(sesion: Sesion, marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> None:
    codigo = leer('Código del marketplace nuevo (o familia-cultura): ').lower()
    if '-' in codigo:
        fam, sufijo = codigo.rsplit('-', 1)
        if fam in familias:
            agregar_cultura(sesion, marketplaces, familias, fam, sufijo)
        else:
            error("El '-' se reserva para culturas de una familia existente.")
        return
    if not re.fullmatch(r'[a-z0-9]+', codigo):
        error('Usa solo letras y números.')
        return
    if codigo in RESERVADAS or codigo in marketplaces or codigo in familias:
        error(f"'{codigo}' ya existe o es una palabra reservada.")
        return
    nombre = pedir_texto('Nombre: ') or codigo.upper()
    currency = pedir_moneda('Moneda [USD]: ', 'USD')
    modo_pago = pedir_modo_pago('puntos_plata')
    punto = pedir_float_opcional('Valor del punto [0]: ', 0.0)
    ta_f, ta_p, fee_f, fee_p, markup_fijo, markup_p = pedir_bloque_calculo()
    agencia_id = pedir_texto('Agencia ID: ')
    url_mkp = pedir_texto('URL marketplace: ')
    url_admin = pedir_texto('URL admin: ')
    usuario = pedir_texto('Usuario: ')
    password = pedir_texto('Password: ')
    marketplaces[codigo] = Agencia(nombre, punto, ta_f, ta_p, fee_f, fee_p, currency, agencia_id,
                                   url_mkp, url_admin, usuario, password, modo_pago, markup_fijo, markup_p)
    marcar_cambio(sesion)
    resultado(f"Marketplace '{codigo}' agregado (se guarda al salir con s).")


def agregar_cultura(sesion: Sesion, marketplaces: dict[str, Agencia], familias: dict[str, Familia],
                    fam: str, sufijo: str) -> None:
    familia = familias[fam]
    if not re.fullmatch(r'[a-z0-9]+', sufijo) or sufijo in familia.culturas:
        error(f"La cultura '{sufijo}' no es válida o ya existe en {familia.nombre}.")
        return
    if sufijo in CAMPOS_EXTRA:
        aviso(f"Aviso: '{fam}-{sufijo}' va a cargar esta cultura, no copiar el campo '{sufijo}'. "
              f"Para el campo usa la forma larga ({fam}-<cultura>-{sufijo}).")
    derivado = nombre_derivado(familia.nombre, sufijo)
    nombre = pedir_texto(f'Nombre [{derivado}]: ', derivado)
    referencia = pedir_texto(f'¿Comparte el bloque de cálculo con otra cultura? '
                             f'({"/".join(familia.culturas)}, Enter = no): ').lower()
    if referencia and referencia not in familia.culturas:
        error('Cultura de referencia desconocida.')
        return
    if referencia:
        ref = marketplaces[f'{fam}-{referencia}']
        calculo = {c: getattr(ref, c) for c in CAMPOS_CALCULO}
    else:
        modo_pago = pedir_modo_pago('puntos_plata')
        punto = pedir_float_opcional('Valor del punto [0]: ', 0.0)
        ta_f, ta_p, fee_f, fee_p, markup_fijo, markup_p = pedir_bloque_calculo()
        calculo = dict(punto=punto, ta_f=ta_f, ta_p=ta_p, fee_f=fee_f, fee_p=fee_p, modo_pago=modo_pago,
                       markup_fijo=markup_fijo, markup_porcentual=markup_p)
    currency = pedir_moneda('Moneda (siempre aparte) [USD]: ', 'USD')
    extras = {CAMPOS_EXTRA[c][0]: pedir_texto(f'{CAMPOS_EXTRA[c][1]}: ') for c in CAMPOS_EXTRA}
    marketplaces[f'{fam}-{sufijo}'] = Agencia(nombre=nombre, currency=currency, **calculo, **extras)
    familia.culturas.append(sufijo)
    marcar_cambio(sesion)
    resultado(f"Cultura '{fam}-{sufijo}' agregada (se guarda al salir con s).")


def elegir_cultura(fam: str, familia: Familia, marketplaces: dict[str, Agencia]) -> str:
    for sufijo in familia.culturas:
        print(linea_menu(marketplaces[f'{fam}-{sufijo}'].nombre, sufijo))
    for _ in range(MAX_INTENTOS):
        sufijo = leer(f'Cultura de {familia.nombre}: ').lower()
        if sufijo in familia.culturas:
            return f'{fam}-{sufijo}'
        error('Cultura desconocida. (x para cancelar)')
    raise Cancelado


# ═══════════════════════════════════════════════════════════════════════
# Tasas de cambio
# ═══════════════════════════════════════════════════════════════════════

class AvisoTasa(Exception):
    pass


# Respaldo si la API no responde: unidades de la moneda por 1 USD (aproximadas, revisar).
TASAS_DEFAULT = {
    'USD': 1.0, 'COP': 4000.0, 'CRC': 510.0, 'GTQ': 7.75, 'HNL': 24.7, 'NIO': 36.6,
    'PAB': 1.0, 'MXN': 18.0, 'EUR': 0.92, 'PEN': 3.75, 'DOP': 60.0, 'CLP': 950.0,
}
URL_TASAS = 'https://open.er-api.com/v6/latest/USD'


class ConversorDivisas:
    """Tasas por moneda, a demanda y solo en memoria. Valor = unidades de la moneda por 1 USD."""

    def __init__(self):
        self._tasas: dict[str, float] = {'USD': 1.0}
        self._forzadas: set[str] = set()
        self._api: dict[str, float] | None = None

    def _consultar_api(self) -> dict[str, float]:
        if self._api is None:
            respuesta = requests.get(URL_TASAS, timeout=5)
            respuesta.raise_for_status()
            self._api = respuesta.json()['rates']
        return self._api

    def tiene(self, moneda: str) -> bool:
        return moneda.upper() in self._tasas

    def convertir_a_USD(self, moneda: str) -> float:
        """Cachea la tasa (API -> default -> 0). No imprime: avisa por excepción."""
        moneda = moneda.upper()
        if moneda in self._tasas:
            return self._tasas[moneda]
        try:
            tasa = float(self._consultar_api()[moneda])
            self._tasas[moneda] = tasa
            return tasa
        except (requests.RequestException, KeyError, ValueError) as e:
            causa = 'moneda no encontrada en la API' if isinstance(e, KeyError) else 'API de tasas no disponible'
            if moneda in TASAS_DEFAULT:
                self._tasas[moneda] = TASAS_DEFAULT[moneda]
                raise AvisoTasa(f'{moneda}: {causa}, se usa la tasa por defecto (revísala).')
            self._tasas[moneda] = 0.0
            raise AvisoTasa(f'{moneda}: {causa} y sin tasa por defecto: cárgala a mano.')

    def obtener_valor(self, moneda: str) -> float:
        return self._tasas.get(moneda.upper(), 0.0)

    def forzar_valor(self, moneda: str, valor: float) -> None:
        self._tasas[moneda.upper()] = valor
        self._forzadas.add(moneda.upper())

    def es_forzada(self, moneda: str) -> bool:
        return moneda.upper() in self._forzadas

    def convertir(self, monto: float, origen: str, destino: str) -> float:
        tasa_origen, tasa_destino = self.obtener_valor(origen), self.obtener_valor(destino)
        if tasa_origen == 0 or tasa_destino == 0:
            raise Cancelado
        return monto / tasa_origen * tasa_destino


def linea_tasa(conversor: ConversorDivisas, moneda: str) -> str:
    valor = conversor.obtener_valor(moneda)
    if valor == 0:
        return f'  {moneda}: sin tasa (cárgala tecleando {moneda.lower()})'
    manual = ' · manual' if conversor.es_forzada(moneda) else ''
    return f'  1 {moneda} = {1 / valor:.6f} USD   (1 USD = {formatear_numero(valor, 4)} {moneda}){manual}'


def revision_tasas(conversor: ConversorDivisas, moneda_1: str, moneda_2: str = '') -> None:
    """Muestra las tasas que se van a aplicar y deja corregirlas. Orden: origen -> destino."""
    originales = [m.upper() for m in (moneda_1, moneda_2) if m]
    monedas = list(dict.fromkeys(m for m in originales if m != 'USD'))
    if not monedas:
        return
    for moneda in monedas:
        if not conversor.tiene(moneda):
            try:
                conversor.convertir_a_USD(moneda)
            except AvisoTasa as e:
                aviso(str(e))
    while True:
        if len(monedas) == 2:
            print(f'Tasas de cambio · {monedas[0]} → USD → {monedas[1]}')
        else:
            print('Tasas de cambio')
        for moneda in monedas:
            print(linea_tasa(conversor, moneda))
        faltantes = [m for m in monedas if conversor.obtener_valor(m) == 0]
        if faltantes:
            entrada = leer(f'Falta tasa de {", ".join(faltantes)}: escribe el código para cargarla (x cancela): ')
        else:
            entrada = leer('Enter para continuar o código de moneda para corregir: ')
            if not entrada:
                return
        moneda = entrada.upper()
        if moneda not in monedas:
            error('Esa moneda no está en este cálculo.')
            continue
        valor = pedir_float_opcional(f'1 USD = ? {moneda} (Enter = sin cambio): ')
        if valor is None:
            continue
        if valor <= 0:
            error('La tasa debe ser mayor a 0.')
            continue
        conversor.forzar_valor(moneda, valor)


# ═══════════════════════════════════════════════════════════════════════
# Calculos (funciones puras: sin input()/print())
# Fórmulas de la primera versión del programa, generalizadas por agencia.
# ═══════════════════════════════════════════════════════════════════════

def puntos_mas_plata(agencia: Agencia, puntos: float, plata: float) -> float:
    """Valor de la reserva en la agencia, en plata."""
    return puntos * agencia.punto + plata


def puntos_mas_plata_puntos(agencia: Agencia, puntos: float, plata: float) -> float:
    """Valor de la reserva en la agencia, expresado solo en puntos."""
    return puntos + plata / agencia.punto


def promo_code(agencia: Agencia, antes: tuple[float, float], despues: tuple[float, float]) -> tuple[float, float]:
    """(descuento fijo, descuento %) entre la reserva sin y con promocode."""
    t1 = puntos_mas_plata(agencia, *antes)
    t2 = puntos_mas_plata(agencia, *despues)
    return t1 - t2, (t1 - t2) * 100 / t1 if t1 else 0.0


def ta_mal_cargada(agencia: Agencia) -> bool:
    """La TA es fija O porcentual, nunca ambas."""
    return agencia.ta_f != 0 and agencia.ta_p != 1


def ta_vuelo(agencia: Agencia, valor_sabre: float, pasajeros: int) -> float:
    """Valor que debería cobrar la agencia por un vuelo de Sabre.

    TA: fija por pasajero O porcentual sobre la base (una sola fórmula: el término inactivo
    queda en su neutro, ta_f=0 o ta_p=1). FEE: fijo por pasajero y/o porcentual sobre la base.
    """
    ta = valor_sabre * agencia.ta_p + agencia.ta_f * pasajeros
    fee = valor_sabre * (agencia.fee_p - 1) + agencia.fee_f * pasajeros
    return ta + fee


def drop_off(mc: float, doc: float, ap: float, europa: bool) -> tuple[float, float]:
    """(valor DropOff, renta en Sabre). En Europa el DOC no hace parte del DropOff."""
    valor = mc if europa else mc + doc
    return valor, ap - valor


def aplicar_markup(agencia: Agencia, precio: float) -> float:
    return precio * agencia.markup_porcentual + agencia.markup_fijo


# ═══════════════════════════════════════════════════════════════════════
# Flujos interactivos
# ═══════════════════════════════════════════════════════════════════════

def avisar_ta_mal_cargada(agencia: Agencia) -> None:
    if ta_mal_cargada(agencia):
        aviso('Aviso: la TA tiene fija y % a la vez; el dato está mal cargado (debe ser una sola).')


def pedir_puntos_y_plata(agencia: Agencia, sufijo: str = '') -> tuple[float, float]:
    """Pide solo lo que aplica según el modo de pago de la agencia."""
    puntos = 0 if agencia.modo_pago == 'solo_plata' else pedir_int(f'Puntos{sufijo}: ')
    plata = 0.0 if agencia.modo_pago == 'solo_puntos' else pedir_float(f'Plata{sufijo} ({agencia.currency}): ')
    return puntos, plata


def comparar_reserva(sesion: Sesion, valor_agencia: float, valor_calculado: float, moneda: str) -> None:
    diferencia = round(valor_agencia - valor_calculado, 2) + 0.0  # + 0.0 evita mostrar '-0'
    if diferencia == 0:
        nota, color = '✔ Coinciden', Fore.GREEN
    else:
        nota, color = f'✘ La agencia cobra de {"más" if diferencia > 0 else "menos"}', Fore.RED
    mostrar_bloque(sesion, vuelos_ta_txt, [('Agencia', valor_agencia, moneda),
                                           ('Calculado desde Sabre', valor_calculado, moneda),
                                           ('Diferencia', diferencia, moneda)], nota, color)


def opcion_bloqueada(opcion: str, agencia: Agencia) -> str:
    if agencia.modo_pago == 'solo_plata':
        return 'Este marketplace es solo plata: no aplica conversión de puntos.' if opcion in ('1', '2') else ''
    if opcion in ('1', '2', '3', '7') and agencia.punto == 0:
        return 'El valor del punto está en 0: cárgalo con edt (campo p).'
    return ''


def ejecutar_opcion(opcion: str, sesion: Sesion, conversor: ConversorDivisas) -> None:
    agencia = sesion.agencia
    moneda = agencia.currency

    if opcion == '1':
        print(f' ■ {puntos_mas_plata_en_plata}')
        revision_rapida(sesion, ['p'])
        puntos, plata = pedir_puntos_y_plata(agencia)
        mostrar_bloque(sesion, puntos_mas_plata_en_plata,
                       [('P+P en solo Plata', puntos_mas_plata(agencia, puntos, plata), moneda)])

    elif opcion == '2':
        print(f' ■ {puntos_mas_plata_en_puntos}')
        revision_rapida(sesion, ['p'])
        if bloqueo := opcion_bloqueada(opcion, agencia):  # la revisión pudo dejar el punto en 0
            error(bloqueo)
            return
        puntos, plata = pedir_puntos_y_plata(agencia)
        mostrar_bloque(sesion, puntos_mas_plata_en_puntos,
                       [('P+P en solo Puntos', round(puntos_mas_plata_puntos(agencia, puntos, plata)), 'puntos')])

    elif opcion == '3':
        print(f' ■ {promo_code_txt}')
        revision_rapida(sesion, ['p'])
        antes = pedir_puntos_y_plata(agencia, ' sin promocode')
        despues = pedir_puntos_y_plata(agencia, ' con promocode')
        fijo, porcentaje = promo_code(agencia, antes, despues)
        mostrar_bloque(sesion, promo_code_txt, [('Descuento porcentual', porcentaje, '%'),
                                                ('Descuento fijo', fijo, moneda)])

    elif opcion == '4':
        print(f' ■ {vuelos_ta_txt}')
        revision_rapida(sesion, ['p', 'tf', 'tp', 'ff', 'fp'])
        avisar_ta_mal_cargada(agencia)
        puntos, plata = pedir_puntos_y_plata(agencia, ' de la reserva')
        valor_sabre = pedir_float(f'Valor del vuelo en Sabre ({moneda}): ')
        pasajeros = pedir_int('Número de pasajeros: ')
        valor_agencia = puntos_mas_plata(agencia, puntos, plata)
        comparar_reserva(sesion, valor_agencia, ta_vuelo(agencia, valor_sabre, pasajeros), moneda)
        moneda_sabre = MONEDA_SABRE_VUELOS.get(sesion.codigo_actual, moneda)
        if moneda_sabre != moneda:
            revision_tasas(conversor, moneda, moneda_sabre)
            mostrar_bloque(sesion, vuelos_ta_txt,
                           [(f'>> Cobro en {moneda_sabre}',
                             conversor.convertir(valor_agencia, moneda, moneda_sabre), moneda_sabre)],
                           'Compáralo con totalAmount de flights/providerFare')

    elif opcion == '5':
        print(f' ■ {dropoff_txt}')
        revision_rapida(sesion, ['p'])
        puntos, plata = pedir_puntos_y_plata(agencia)
        mc = pedir_float('MC de Sabre (USD): ')
        doc = pedir_float('DOC de Sabre (USD): ')
        ap = pedir_float('AP de Sabre (USD): ')
        europa = confirmar('¿La reserva es en Europa? (s/n): ')
        valor_agencia = puntos_mas_plata(agencia, puntos, plata)
        if moneda != 'USD':
            revision_tasas(conversor, moneda, 'USD')
            print(f'  Agencia: {monto(valor_agencia, moneda)}')
            valor_agencia = conversor.convertir(valor_agencia, moneda, 'USD')
        valor_drop, renta = drop_off(mc, doc, ap, europa)
        mostrar_bloque(sesion, dropoff_txt, [('Reserva en Agencia', valor_agencia, 'USD'),
                                             ('Reserva en Sabre', renta, 'USD'),
                                             ('DropOff', valor_drop, 'USD')],
                       'Europa: solo MC' if europa else 'MC + DOC')

    elif opcion == '6':
        print(f' ■ {markup_txt}')
        revision_rapida(sesion, ['mkf', 'mkp'])
        precio = pedir_float('Precio del proveedor: ')
        if confirmar(f'¿El precio está en una moneda diferente a {moneda}? (s/n): '):
            moneda_precio = pedir_moneda(f'Moneda del precio [{moneda}]: ', moneda)
            revision_tasas(conversor, moneda_precio, moneda)
            precio = conversor.convertir(precio, moneda_precio, moneda)
            print(f'  Precio convertido: {monto(precio, moneda)}')
        total = aplicar_markup(agencia, precio)
        mostrar_bloque(sesion, markup_txt, [('Precio', precio, moneda), ('MarkUp', total - precio, moneda),
                                            ('Con MarkUp', total, moneda)])

    elif opcion == '7':
        print(f' ■ {puntos_mas_plata_a_otra_moneda}')
        revision_rapida(sesion, ['p'])
        puntos, plata = pedir_puntos_y_plata(agencia)
        total = puntos_mas_plata(agencia, puntos, plata)
        destino = pedir_moneda('Moneda destino [USD]: ', 'USD')
        revision_tasas(conversor, moneda, destino)
        convertido = conversor.convertir(total, moneda, destino)
        mostrar_bloque(sesion, puntos_mas_plata_a_otra_moneda, [('P+P', total, moneda),
                                                                ('Convertido', convertido, destino)])


def eliminar_marketplace(sesion: Sesion, marketplaces: dict[str, Agencia], familias: dict[str, Familia]) -> None:
    entrada = leer('Código a eliminar (agencia, familia o familia-cultura): ')
    codigo = resolver_agencia(entrada, marketplaces, familias)
    if not codigo:
        error(f"'{entrada}' no es un marketplace.")
        return
    if codigo in familias:
        codigos = [f'{codigo}-{s}' for s in familias[codigo].culturas]
        descripcion = f'la familia {familias[codigo].nombre} completa ({len(codigos)} culturas)'
    else:
        codigos = [codigo]
        descripcion = marketplaces[codigo].nombre
    if not confirmar(f'¿Eliminar {descripcion}? Se borra del JSON al guardar al salir (s/n): '):
        aviso('No se eliminó nada.')
        return
    familia = familia_de(codigo, familias)
    for c in codigos:
        del marketplaces[c]
    if codigo in familias:
        del familias[codigo]
    elif familia:
        familias[familia].culturas.remove(codigo.rsplit('-', 1)[1])
        if not familias[familia].culturas:
            del familias[familia]
    if sesion.codigo_actual in codigos:
        sesion.codigo_actual, sesion.agencia = '', None
    marcar_cambio(sesion)
    resultado(f'{descripcion} eliminado (se guarda al salir con s).')


def validar_catalogo(marketplaces: dict[str, Agencia]) -> None:
    """Avisa de datos mal cargados al iniciar: TA doble o agencia_id repetido."""
    vistos: dict[str, str] = {}
    for codigo, agencia in marketplaces.items():
        if ta_mal_cargada(agencia):
            aviso(f'Aviso {codigo}: la TA tiene fija y % a la vez (debe ser una sola).')
        if agencia.agencia_id:
            if agencia.agencia_id in vistos:
                aviso(f'Aviso: {codigo} y {vistos[agencia.agencia_id]} tienen el mismo agencia_id.')
            vistos.setdefault(agencia.agencia_id, codigo)


def cargar_agencia(sesion: Sesion, marketplaces: dict[str, Agencia], codigo: str) -> None:
    sesion.codigo_actual = codigo
    sesion.agencia = marketplaces[codigo]


def procesar_codigo(entrada: str, sesion: Sesion, marketplaces: dict[str, Agencia],
                    familias: dict[str, Familia]) -> None:
    codigo, campo = resolver_codigo(entrada, marketplaces, familias)
    if not codigo:
        error(f"'{entrada}' no es un comando ni un marketplace. (m = menú, 0 = lista de agencias)")
        return
    if codigo in familias:
        codigo = elegir_cultura(codigo, familias[codigo], marketplaces)
    cargar_agencia(sesion, marketplaces, codigo)
    if campo:
        mostrar_dato_extra(sesion.agencia, campo)  # salida compacta: sin ficha
    else:
        mostrar_ficha(sesion.agencia)


def main() -> None:
    colorama_init()
    marketplaces, familias = cargar_marketplaces(RUTA_MARKETPLACES)
    sesion = Sesion()
    conversor = ConversorDivisas()
    validar_catalogo(marketplaces)
    menu()
    while True:
        etiqueta = sesion.codigo_actual.upper() or 'sin agencia'
        try:
            entrada = input(f'{Fore.CYAN}{Style.BRIGHT}[{etiqueta}]{Style.RESET_ALL} => ').strip()
        except EOFError:
            print()
            entrada = 's'
        opcion = entrada.lower()
        if not opcion:
            continue
        try:
            if opcion == 's':
                if sesion.cambios_pendientes and confirmar(
                        'Hay cambios sin guardar. ¿Guardar en marketplaces.json? (s/n): '):
                    guardar_marketplaces(RUTA_MARKETPLACES, marketplaces, familias)
                    resultado('Cambios guardados.')
                break
            elif opcion == 'm':
                menu()
            elif opcion == '0':
                menu_agencias(marketplaces, familias)
            elif opcion == 'new':
                agregar_marketplace(sesion, marketplaces, familias)
            elif opcion == 'del':
                eliminar_marketplace(sesion, marketplaces, familias)
            elif opcion == 'c':
                copiar_ultimo_resultado(sesion)
            elif opcion == 'cls':
                os.system('cls' if os.name == 'nt' else 'clear')
            elif opcion == 'jdra':
                print('Este Programa fue creado por:\n Jeferson Daniel Romero Acosta\n'
                      ' https://github.com/jefferdaniel-dev\n 2024')
            elif opcion in COMANDOS or opcion in CAMPOS_EXTRA:
                if sesion.agencia is None:
                    error('Primero carga una agencia (escribe su código; 0 = lista de agencias).')
                elif opcion == 'edt':
                    editar_agencia(sesion)
                elif opcion in CAMPOS_EXTRA:
                    mostrar_dato_extra(sesion.agencia, opcion)
                elif bloqueo := opcion_bloqueada(opcion, sesion.agencia):
                    error(bloqueo)
                else:
                    ejecutar_opcion(opcion, sesion, conversor)
            else:
                procesar_codigo(entrada, sesion, marketplaces, familias)
        except Cancelado:
            aviso('Cancelado.')
        except EOFError:
            print()
            break


if __name__ == '__main__':
    main()
