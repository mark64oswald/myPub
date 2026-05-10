"""healthcare_libs.integration_channel_xml — Mirth/OIE/BridgeLink channel.xml builder.

NextGen Connect (Mirth) 4.x, Open Integration Engine (OIE) 4.5.x, and
BridgeLink Connect all share the same channel.xml schema. This module
builds channel XML that imports cleanly into Channel Manager — every
required element is present, the `class=` attributes on connector
properties match the Java property classes the runtime expects, and
operator-replaceable values are clearly marked with sentinel tokens
(``_REPLACE_WITH_HOST_``, ``_REPLACE_WITH_PORT_``, etc.) so the
operator knows what to fill in.

Why our own builder rather than using a Mirth client library? The
runtime's java client jar is heavy (10+ MB) and only useful for live
deploys. For generating import-ready XML deterministically, we just
need the right tag tree + the right class attributes — straight string
templates do that fine and stay portable across the three target
engines.

Public API:

  * :class:`SourceConfig` — source connector parameters (LLP/HTTP/Database/DICOM listener)
  * :class:`DestConfig` — destination connector parameters (LLP/HTTP/File/JS sender)
  * :func:`build_channel` — assemble a complete channel.xml string

Sentinel tokens (replace before deploying):

  * ``_REPLACE_WITH_HOST_`` — destination host
  * ``_REPLACE_WITH_PORT_`` — destination port (when not provided)
  * ``_REPLACE_WITH_URL_`` — HTTP destination URL
  * ``_REPLACE_WITH_USERNAME_``, ``_REPLACE_WITH_PASSWORD_`` — credentials
  * ``_REPLACE_WITH_PATH_`` — file/SFTP path

Reference (verified import targets):
  * NextGen Connect 4.4.x channel schema (channelExport.xsd)
  * Open Integration Engine 4.5.x — same schema as upstream Mirth 4.x
  * BridgeLink Connect — same schema as Mirth 4.x
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional
from xml.sax.saxutils import escape as xml_escape

CHANNEL_VERSION = "4.4.0"

# Sentinel tokens an operator must replace before deployment.
SENTINEL_HOST = "_REPLACE_WITH_HOST_"
SENTINEL_PORT = "_REPLACE_WITH_PORT_"
SENTINEL_URL = "_REPLACE_WITH_URL_"
SENTINEL_USERNAME = "_REPLACE_WITH_USERNAME_"
SENTINEL_PASSWORD = "_REPLACE_WITH_PASSWORD_"
SENTINEL_PATH = "_REPLACE_WITH_PATH_"


# Map our friendly connector_type to the Java properties class Mirth/OIE
# expects on the <properties class="..."> attribute. These class names
# are part of the runtime ABI — changing them breaks import.
SOURCE_PROPS_CLASS = {
    "LLP Listener":      "com.mirth.connect.connectors.tcp.TcpReceiverProperties",
    "TCP Listener":      "com.mirth.connect.connectors.tcp.TcpReceiverProperties",
    "HTTP Listener":     "com.mirth.connect.connectors.http.HttpReceiverProperties",
    "HTTP Listener (FHIR)": "com.mirth.connect.connectors.http.HttpReceiverProperties",
    "Database Reader":   "com.mirth.connect.connectors.jdbc.DatabaseReceiverProperties",
    "DICOM Listener":    "com.mirth.connect.connectors.dimse.DICOMReceiverProperties",
    "DICOM SCP (Service Class Provider)": "com.mirth.connect.connectors.dimse.DICOMReceiverProperties",
    "Channel Reader":    "com.mirth.connect.donkey.model.channel.SourceConnectorPropertiesInterface",
}

DEST_PROPS_CLASS = {
    "LLP Sender":        "com.mirth.connect.connectors.tcp.TcpDispatcherProperties",
    "TCP Sender":        "com.mirth.connect.connectors.tcp.TcpDispatcherProperties",
    "HTTP Sender":       "com.mirth.connect.connectors.http.HttpDispatcherProperties",
    "File Writer":       "com.mirth.connect.connectors.file.FileDispatcherProperties",
    "File Writer (SFTP)": "com.mirth.connect.connectors.file.FileDispatcherProperties",
    "Database Writer":   "com.mirth.connect.connectors.jdbc.DatabaseDispatcherProperties",
    "JavaScript Writer": "com.mirth.connect.connectors.js.JavaScriptDispatcherProperties",
    "Channel Writer":    "com.mirth.connect.connectors.vm.VmDispatcherProperties",
    "DICOM Sender":      "com.mirth.connect.connectors.dimse.DICOMDispatcherProperties",
    "AS2 Sender":        "com.mirth.connect.connectors.http.HttpDispatcherProperties",
}

# Plugin/transmission-mode classes — TCP/LLP variants need this nested element.
TRANSMISSION_MODES = {
    "LLP Listener": "MLLP",
    "LLP Sender": "MLLP",
    "TCP Listener": "Basic",
    "TCP Sender": "Basic",
}


@dataclass
class SourceConfig:
    """Parameters for the source (inbound) connector.

    ``connector_type`` must be one of the keys of :data:`SOURCE_PROPS_CLASS`.
    """

    connector_type: str          # e.g. "LLP Listener", "HTTP Listener", "Database Reader", "DICOM Listener"
    message_format: str          # e.g. "HL7v2", "FHIR JSON", "X12 EDI", "DICOM Part 10"
    host: str = "0.0.0.0"        # bind address — usually 0.0.0.0 to listen on all interfaces
    port: int = 0                # 0 means use the sentinel
    url_path: str = ""           # for HTTP listener: relative path (e.g. "/fhir/R4/AdverseEvent")
    polling_query: str = ""      # for Database Reader
    polling_frequency_ms: int = 60_000
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DestConfig:
    """Parameters for one destination (outbound) connector."""

    name: str                    # display name for the destination
    connector_type: str          # e.g. "LLP Sender", "HTTP Sender", "File Writer (SFTP)"
    message_format: str
    host: str = SENTINEL_HOST
    port: int = 0                # 0 → sentinel
    url: str = SENTINEL_URL      # for HTTP destinations
    method: str = "POST"
    file_path: str = SENTINEL_PATH
    username: str = SENTINEL_USERNAME
    password: str = SENTINEL_PASSWORD
    transformer_js: str = ""     # JS body for this destination's transformer (optional)
    extra: dict[str, Any] = field(default_factory=dict)


def _stable_uuid(seed: str) -> str:
    """Deterministic UUID-shaped id from a seed string.

    Mirth's <id> field accepts any string but most tools assume UUID
    formatting — derive one from the channel name so re-imports remain
    idempotent.
    """
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _esc(s: Any) -> str:
    """XML-escape; coerce to string first."""
    return xml_escape(str(s) if s is not None else "")


def _port_or_sentinel(p: int) -> str:
    return str(p) if p and p > 0 else SENTINEL_PORT


def _xml_cdata(text: str) -> str:
    """Wrap a script body in CDATA, escaping any embedded ``]]>`` defensively."""
    if not text:
        return ""
    safe = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


# ---------------------------------------------------------------------------
# Source connector property blocks
# ---------------------------------------------------------------------------

def _source_props_llp(src: SourceConfig) -> str:
    port = _port_or_sentinel(src.port)
    return f"""\
      <listenerConnectorProperties>
        <host>{_esc(src.host)}</host>
        <port>{port}</port>
      </listenerConnectorProperties>
      <sourceConnectorProperties version="{CHANNEL_VERSION}">
        <responseVariable>None</responseVariable>
        <respondAfterProcessing>true</respondAfterProcessing>
        <processBatch>false</processBatch>
        <firstResponse>false</firstResponse>
        <processingThreads>1</processingThreads>
        <resourceIds class="linked-hash-map">
          <entry>
            <string>Default Resource</string>
            <string>[Default Resource]</string>
          </entry>
        </resourceIds>
        <queueBufferSize>1000</queueBufferSize>
      </sourceConnectorProperties>
      <transmissionModeProperties class="com.mirth.connect.plugins.mllpmode.MLLPModeProperties">
        <pluginPointName>MLLP</pluginPointName>
        <startOfMessageBytes>0B</startOfMessageBytes>
        <endOfMessageBytes>1C0D</endOfMessageBytes>
      </transmissionModeProperties>
      <serverMode>true</serverMode>
      <remoteAddress></remoteAddress>
      <remotePort></remotePort>
      <overrideLocalBinding>false</overrideLocalBinding>
      <reconnectInterval>5000</reconnectInterval>
      <receiveTimeout>0</receiveTimeout>
      <bufferSize>65536</bufferSize>
      <maxConnections>10</maxConnections>
      <keepConnectionOpen>true</keepConnectionOpen>
      <dataTypeBinary>false</dataTypeBinary>
      <charsetEncoding>DEFAULT_ENCODING</charsetEncoding>
      <respondOnNewConnection>0</respondOnNewConnection>
      <responseAddress></responseAddress>
      <responsePort></responsePort>"""


def _source_props_http(src: SourceConfig) -> str:
    port = _port_or_sentinel(src.port or 8081)
    url_path = src.url_path or "/"
    return f"""\
      <listenerConnectorProperties>
        <host>{_esc(src.host)}</host>
        <port>{port}</port>
      </listenerConnectorProperties>
      <sourceConnectorProperties version="{CHANNEL_VERSION}">
        <responseVariable>None</responseVariable>
        <respondAfterProcessing>true</respondAfterProcessing>
        <processBatch>false</processBatch>
        <firstResponse>false</firstResponse>
        <processingThreads>1</processingThreads>
        <resourceIds class="linked-hash-map">
          <entry>
            <string>Default Resource</string>
            <string>[Default Resource]</string>
          </entry>
        </resourceIds>
        <queueBufferSize>1000</queueBufferSize>
      </sourceConnectorProperties>
      <xmlBody>false</xmlBody>
      <parseMultipart>true</parseMultipart>
      <includeMetadata>false</includeMetadata>
      <binaryMimeTypesRegex>true</binaryMimeTypesRegex>
      <binaryMimeTypes>application/.*(?&lt;!json|xml)$|image/.*|video/.*|audio/.*</binaryMimeTypes>
      <responseContentType>application/json</responseContentType>
      <responseDataTypeBinary>false</responseDataTypeBinary>
      <responseStatusCode></responseStatusCode>
      <responseHeaders class="linked-hash-map"/>
      <charset>UTF-8</charset>
      <contextPath>{_esc(url_path)}</contextPath>
      <timeout>30000</timeout>"""


def _source_props_database(src: SourceConfig) -> str:
    query = src.polling_query or "SELECT * FROM inbox WHERE status = 'READY'"
    return f"""\
      <pollConnectorProperties version="{CHANNEL_VERSION}">
        <pollingType>INTERVAL</pollingType>
        <pollOnStart>true</pollOnStart>
        <pollingFrequency>{src.polling_frequency_ms}</pollingFrequency>
        <pollingHour>0</pollingHour>
        <pollingMinute>0</pollingMinute>
        <cronJobs/>
      </pollConnectorProperties>
      <sourceConnectorProperties version="{CHANNEL_VERSION}">
        <responseVariable>None</responseVariable>
        <respondAfterProcessing>true</respondAfterProcessing>
        <processBatch>false</processBatch>
        <firstResponse>false</firstResponse>
        <processingThreads>1</processingThreads>
        <resourceIds class="linked-hash-map">
          <entry>
            <string>Default Resource</string>
            <string>[Default Resource]</string>
          </entry>
        </resourceIds>
        <queueBufferSize>1000</queueBufferSize>
      </sourceConnectorProperties>
      <driver>{_esc(src.extra.get("driver", "org.postgresql.Driver"))}</driver>
      <url>{_esc(src.extra.get("jdbc_url", "jdbc:postgresql://" + SENTINEL_HOST + ":5432/billing"))}</url>
      <username>{SENTINEL_USERNAME}</username>
      <password>{SENTINEL_PASSWORD}</password>
      <select>{_esc(query)}</select>
      <update></update>
      <useScript>false</useScript>
      <aggregateResults>false</aggregateResults>
      <cacheResults>true</cacheResults>
      <keepConnectionOpen>true</keepConnectionOpen>
      <updateMode>1</updateMode>
      <retryCount>3</retryCount>
      <retryInterval>10000</retryInterval>
      <fetchSize>1000</fetchSize>
      <encoding>DEFAULT_ENCODING</encoding>"""


def _source_props_dicom(src: SourceConfig) -> str:
    port = _port_or_sentinel(src.port or 11112)
    return f"""\
      <listenerConnectorProperties>
        <host>{_esc(src.host)}</host>
        <port>{port}</port>
      </listenerConnectorProperties>
      <sourceConnectorProperties version="{CHANNEL_VERSION}">
        <responseVariable>None</responseVariable>
        <respondAfterProcessing>true</respondAfterProcessing>
        <processBatch>false</processBatch>
        <firstResponse>false</firstResponse>
        <processingThreads>1</processingThreads>
        <resourceIds class="linked-hash-map">
          <entry>
            <string>Default Resource</string>
            <string>[Default Resource]</string>
          </entry>
        </resourceIds>
        <queueBufferSize>1000</queueBufferSize>
      </sourceConnectorProperties>
      <applicationEntity></applicationEntity>
      <localApplicationEntity></localApplicationEntity>
      <soCloseDelay>50</soCloseDelay>
      <releaseTo>5</releaseTo>
      <requestTo>5</requestTo>
      <idleTo>60</idleTo>
      <reaper>10</reaper>
      <rspDelay>0</rspDelay>
      <pdv1>false</pdv1>
      <sndpdulen>16</sndpdulen>
      <rcvpdulen>16</rcvpdulen>
      <async>0</async>
      <bigEndian>false</bigEndian>
      <defts>false</defts>
      <nativeData>false</nativeData>
      <sorcvbuf>0</sorcvbuf>
      <sosndbuf>0</sosndbuf>
      <tcpDelay>true</tcpDelay>
      <tls>notls</tls>
      <auth>true</auth>
      <iv>true</iv>
      <keyPW></keyPW>
      <keyStore></keyStore>
      <keyStorePW></keyStorePW>
      <noClientAuth>true</noClientAuth>
      <nossl2>true</nossl2>
      <trustStore></trustStore>
      <trustStorePW></trustStorePW>"""


_SOURCE_BUILDERS = {
    "LLP Listener": _source_props_llp,
    "TCP Listener": _source_props_llp,
    "HTTP Listener": _source_props_http,
    "HTTP Listener (FHIR)": _source_props_http,
    "Database Reader": _source_props_database,
    "DICOM Listener": _source_props_dicom,
    "DICOM SCP (Service Class Provider)": _source_props_dicom,
}


# ---------------------------------------------------------------------------
# Destination connector property blocks
# ---------------------------------------------------------------------------

def _dest_props_llp(dst: DestConfig) -> str:
    port = _port_or_sentinel(dst.port)
    return f"""\
        <destinationConnectorProperties version="{CHANNEL_VERSION}">
          <queueEnabled>false</queueEnabled>
          <sendFirst>false</sendFirst>
          <retryIntervalMillis>10000</retryIntervalMillis>
          <regenerateTemplate>false</regenerateTemplate>
          <retryCount>0</retryCount>
          <rotate>false</rotate>
          <includeFilterTransformer>false</includeFilterTransformer>
          <threadCount>1</threadCount>
          <threadAssignmentVariable></threadAssignmentVariable>
          <validateResponse>false</validateResponse>
          <resourceIds class="linked-hash-map">
            <entry>
              <string>Default Resource</string>
              <string>[Default Resource]</string>
            </entry>
          </resourceIds>
          <queueBufferSize>1000</queueBufferSize>
          <reattachAttachments>true</reattachAttachments>
        </destinationConnectorProperties>
        <transmissionModeProperties class="com.mirth.connect.plugins.mllpmode.MLLPModeProperties">
          <pluginPointName>MLLP</pluginPointName>
          <startOfMessageBytes>0B</startOfMessageBytes>
          <endOfMessageBytes>1C0D</endOfMessageBytes>
        </transmissionModeProperties>
        <remoteAddress>{_esc(dst.host)}</remoteAddress>
        <remotePort>{port}</remotePort>
        <overrideLocalBinding>false</overrideLocalBinding>
        <localAddress>0.0.0.0</localAddress>
        <localPort>0</localPort>
        <sendTimeout>5000</sendTimeout>
        <bufferSize>65536</bufferSize>
        <keepConnectionOpen>true</keepConnectionOpen>
        <checkRemoteHost>true</checkRemoteHost>
        <responseTimeout>5000</responseTimeout>
        <ignoreResponse>false</ignoreResponse>
        <queueOnResponseTimeout>true</queueOnResponseTimeout>
        <dataTypeBinary>false</dataTypeBinary>
        <charsetEncoding>DEFAULT_ENCODING</charsetEncoding>
        <template>${{message.encodedData}}</template>"""


def _dest_props_http(dst: DestConfig) -> str:
    return f"""\
        <destinationConnectorProperties version="{CHANNEL_VERSION}">
          <queueEnabled>false</queueEnabled>
          <sendFirst>false</sendFirst>
          <retryIntervalMillis>10000</retryIntervalMillis>
          <regenerateTemplate>false</regenerateTemplate>
          <retryCount>0</retryCount>
          <rotate>false</rotate>
          <includeFilterTransformer>false</includeFilterTransformer>
          <threadCount>1</threadCount>
          <threadAssignmentVariable></threadAssignmentVariable>
          <validateResponse>false</validateResponse>
          <resourceIds class="linked-hash-map">
            <entry>
              <string>Default Resource</string>
              <string>[Default Resource]</string>
            </entry>
          </resourceIds>
          <queueBufferSize>1000</queueBufferSize>
          <reattachAttachments>true</reattachAttachments>
        </destinationConnectorProperties>
        <host>{_esc(dst.url)}</host>
        <useProxyServer>false</useProxyServer>
        <proxyAddress></proxyAddress>
        <proxyPort></proxyPort>
        <method>{_esc(dst.method)}</method>
        <headers class="linked-hash-map">
          <entry>
            <string>Content-Type</string>
            <string>{_esc(_content_type_for(dst.message_format))}</string>
          </entry>
          <entry>
            <string>Authorization</string>
            <string>Bearer {SENTINEL_PASSWORD}</string>
          </entry>
        </headers>
        <parameters class="linked-hash-map"/>
        <responseXmlBody>false</responseXmlBody>
        <responseParseMultipart>true</responseParseMultipart>
        <responseIncludeMetadata>false</responseIncludeMetadata>
        <responseBinaryMimeTypesRegex>true</responseBinaryMimeTypesRegex>
        <responseBinaryMimeTypes>application/.*(?&lt;!json|xml)$|image/.*|video/.*|audio/.*</responseBinaryMimeTypes>
        <multipart>false</multipart>
        <useAuthentication>false</useAuthentication>
        <authenticationType>Basic</authenticationType>
        <usePreemptiveAuthentication>false</usePreemptiveAuthentication>
        <username>{SENTINEL_USERNAME}</username>
        <password>{SENTINEL_PASSWORD}</password>
        <content>${{message.encodedData}}</content>
        <contentType>{_esc(_content_type_for(dst.message_format))}</contentType>
        <dataTypeBinary>false</dataTypeBinary>
        <charset>UTF-8</charset>
        <socketTimeout>30000</socketTimeout>"""


def _dest_props_file(dst: DestConfig) -> str:
    scheme = "sftp" if "SFTP" in dst.connector_type.upper() else "file"
    return f"""\
        <destinationConnectorProperties version="{CHANNEL_VERSION}">
          <queueEnabled>false</queueEnabled>
          <sendFirst>false</sendFirst>
          <retryIntervalMillis>10000</retryIntervalMillis>
          <regenerateTemplate>false</regenerateTemplate>
          <retryCount>0</retryCount>
          <rotate>false</rotate>
          <includeFilterTransformer>false</includeFilterTransformer>
          <threadCount>1</threadCount>
          <threadAssignmentVariable></threadAssignmentVariable>
          <validateResponse>false</validateResponse>
          <resourceIds class="linked-hash-map">
            <entry>
              <string>Default Resource</string>
              <string>[Default Resource]</string>
            </entry>
          </resourceIds>
          <queueBufferSize>1000</queueBufferSize>
          <reattachAttachments>true</reattachAttachments>
        </destinationConnectorProperties>
        <scheme>{scheme}</scheme>
        <schemeProperties class="com.mirth.connect.connectors.file.SftpSchemeProperties">
          <passwordAuth>true</passwordAuth>
          <keyAuth>false</keyAuth>
          <keyFile></keyFile>
          <passPhrase></passPhrase>
          <hostKeyChecking>no</hostKeyChecking>
          <knownHostsFile></knownHostsFile>
          <configurationSettings class="linked-hash-map"/>
        </schemeProperties>
        <host>{_esc(dst.host)}</host>
        <outputPattern>${{originalFilename}}</outputPattern>
        <anonymous>false</anonymous>
        <username>{SENTINEL_USERNAME}</username>
        <password>{SENTINEL_PASSWORD}</password>
        <timeout>10000</timeout>
        <keepConnectionOpen>true</keepConnectionOpen>
        <maxIdleTime>0</maxIdleTime>
        <secure>true</secure>
        <passive>true</passive>
        <validateConnection>true</validateConnection>
        <outputAppend>false</outputAppend>
        <errorOnExists>false</errorOnExists>
        <temporary>false</temporary>
        <binary>false</binary>
        <charsetEncoding>UTF-8</charsetEncoding>
        <template>${{message.encodedData}}</template>"""


def _dest_props_javascript(dst: DestConfig) -> str:
    return f"""\
        <destinationConnectorProperties version="{CHANNEL_VERSION}">
          <queueEnabled>false</queueEnabled>
          <sendFirst>false</sendFirst>
          <retryIntervalMillis>10000</retryIntervalMillis>
          <regenerateTemplate>false</regenerateTemplate>
          <retryCount>0</retryCount>
          <rotate>false</rotate>
          <includeFilterTransformer>false</includeFilterTransformer>
          <threadCount>1</threadCount>
          <threadAssignmentVariable></threadAssignmentVariable>
          <validateResponse>false</validateResponse>
          <resourceIds class="linked-hash-map">
            <entry>
              <string>Default Resource</string>
              <string>[Default Resource]</string>
            </entry>
          </resourceIds>
          <queueBufferSize>1000</queueBufferSize>
          <reattachAttachments>true</reattachAttachments>
        </destinationConnectorProperties>
        <script>{_xml_cdata(dst.transformer_js or "// JavaScript Writer body — return the string to write\nreturn $('encodedData') || $('rawData') || '';\n")}</script>"""


def _dest_props_database(dst: DestConfig) -> str:
    return f"""\
        <destinationConnectorProperties version="{CHANNEL_VERSION}">
          <queueEnabled>false</queueEnabled>
          <sendFirst>false</sendFirst>
          <retryIntervalMillis>10000</retryIntervalMillis>
          <regenerateTemplate>false</regenerateTemplate>
          <retryCount>0</retryCount>
          <rotate>false</rotate>
          <includeFilterTransformer>false</includeFilterTransformer>
          <threadCount>1</threadCount>
          <threadAssignmentVariable></threadAssignmentVariable>
          <validateResponse>false</validateResponse>
          <resourceIds class="linked-hash-map">
            <entry>
              <string>Default Resource</string>
              <string>[Default Resource]</string>
            </entry>
          </resourceIds>
          <queueBufferSize>1000</queueBufferSize>
          <reattachAttachments>true</reattachAttachments>
        </destinationConnectorProperties>
        <driver>{_esc(dst.extra.get("driver", "org.postgresql.Driver"))}</driver>
        <url>{_esc(dst.extra.get("jdbc_url", "jdbc:postgresql://" + SENTINEL_HOST + ":5432/destination"))}</url>
        <username>{SENTINEL_USERNAME}</username>
        <password>{SENTINEL_PASSWORD}</password>
        <query>{_esc(dst.extra.get("query", "INSERT INTO outbox (payload) VALUES (?)"))}</query>
        <useScript>false</useScript>"""


_DEST_BUILDERS = {
    "LLP Sender": _dest_props_llp,
    "TCP Sender": _dest_props_llp,
    "HTTP Sender": _dest_props_http,
    "AS2 Sender": _dest_props_http,
    "File Writer": _dest_props_file,
    "File Writer (SFTP)": _dest_props_file,
    "JavaScript Writer": _dest_props_javascript,
    "Database Writer": _dest_props_database,
}


def _content_type_for(message_format: str) -> str:
    fmt = (message_format or "").lower()
    if "fhir" in fmt or "json" in fmt:
        return "application/fhir+json"
    if "xml" in fmt or "e2b" in fmt:
        return "application/xml"
    if "x12" in fmt or "edi" in fmt:
        return "application/EDI-X12"
    if "hl7" in fmt:
        return "application/hl7-v2"
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def _data_type_for(message_format: str) -> str:
    """Map a friendly format label to Mirth's data type name."""
    fmt = (message_format or "").upper()
    if "HL7" in fmt:
        return "HL7V2"
    if "FHIR" in fmt or "JSON" in fmt:
        return "JSON"
    if "X12" in fmt or "EDI" in fmt:
        return "EDI/X12"
    if "DICOM" in fmt:
        return "DICOM"
    if "XML" in fmt:
        return "XML"
    return "RAW"


def _data_type_block(message_format: str, *, inbound: bool = True) -> str:
    """Render a <transportName>... wrapped data-type block.

    Mirth requires both inbound and outbound data type config on every
    connector — we pick a sensible default per format.
    """
    dt = _data_type_for(message_format)
    return f"""\
        <type>{dt}</type>
        <properties class="com.mirth.connect.plugins.datatypes.raw.RawDataTypeProperties"/>"""


def _filter_block() -> str:
    return """\
      <filter version=\"4.4.0\">
        <elements/>
      </filter>"""


def _transformer_block(js_body: str, *, inbound_format: str, outbound_format: str) -> str:
    """Render a transformer with one JavaScript step containing the supplied body."""
    inbound_dt = _data_type_for(inbound_format)
    outbound_dt = _data_type_for(outbound_format)
    if not js_body.strip():
        elements = ""
    else:
        elements = f"""
          <com.mirth.connect.plugins.javascriptstep.JavaScriptStep version="{CHANNEL_VERSION}">
            <name>Transform</name>
            <sequenceNumber>0</sequenceNumber>
            <enabled>true</enabled>
            <script>{_xml_cdata(js_body)}</script>
          </com.mirth.connect.plugins.javascriptstep.JavaScriptStep>"""
    return f"""\
      <transformer version="{CHANNEL_VERSION}">
        <elements>{elements}
        </elements>
        <inboundDataType>{inbound_dt}</inboundDataType>
        <outboundDataType>{outbound_dt}</outboundDataType>
        <inboundProperties class="com.mirth.connect.plugins.datatypes.raw.RawDataTypeProperties"/>
        <outboundProperties class="com.mirth.connect.plugins.datatypes.raw.RawDataTypeProperties"/>
      </transformer>"""


def build_channel(
    *,
    name: str,
    description: str,
    source: SourceConfig,
    destinations: list[DestConfig],
    transformer_js: str = "",
    channel_id: Optional[str] = None,
    engine_target: str = "Mirth/OIE/BridgeLink",
) -> str:
    """Build a Mirth/OIE/BridgeLink-compatible channel.xml string.

    The result imports cleanly into NextGen Connect 4.x, Open Integration
    Engine 4.5.x, and BridgeLink Connect 4.x — all three share the same
    schema. Operator-replaceable fields (host, credentials, paths) carry
    sentinel values so the operator knows what to fill in before deploy.

    Parameters
    ----------
    name : channel name (shown in Channel Manager)
    description : human-readable description
    source : :class:`SourceConfig` for the inbound connector
    destinations : list of :class:`DestConfig` for outbound connectors (≥1)
    transformer_js : JS body for the source-side transformer (runs once
        per inbound message before destinations are dispatched)
    channel_id : optional explicit channel id; defaults to a deterministic
        UUID derived from the name
    engine_target : metadata-only — informational comment in the XML

    Returns
    -------
    A well-formed channel.xml string ready to feed Mirth's channelImport
    endpoint or to drop into Channel Manager → Import Channel.
    """
    if not destinations:
        raise ValueError("at least one destination is required")

    cid = channel_id or _stable_uuid(name)
    next_meta = len(destinations) + 1
    src_class = SOURCE_PROPS_CLASS.get(source.connector_type)
    if src_class is None:
        raise ValueError(
            f"unsupported source connector_type {source.connector_type!r}; "
            f"known: {sorted(SOURCE_PROPS_CLASS)}"
        )
    src_builder = _SOURCE_BUILDERS.get(source.connector_type)
    if src_builder is None:
        raise ValueError(
            f"no source property builder for {source.connector_type!r}"
        )

    src_props_xml = src_builder(source)
    src_transformer_xml = _transformer_block(
        transformer_js,
        inbound_format=source.message_format,
        outbound_format=destinations[0].message_format,
    )

    dest_blocks: list[str] = []
    for i, dst in enumerate(destinations, start=1):
        dst_class = DEST_PROPS_CLASS.get(dst.connector_type)
        if dst_class is None:
            raise ValueError(
                f"unsupported destination connector_type {dst.connector_type!r}; "
                f"known: {sorted(DEST_PROPS_CLASS)}"
            )
        dst_builder = _DEST_BUILDERS.get(dst.connector_type)
        if dst_builder is None:
            raise ValueError(
                f"no destination property builder for {dst.connector_type!r}"
            )
        dst_props_xml = dst_builder(dst)
        # Per-destination transformer slot — empty by default (the source
        # transformer already produced the destination payload). If the
        # caller supplied dst.transformer_js we drop it here.
        dst_xform = _transformer_block(
            dst.transformer_js,
            inbound_format=destinations[0].message_format,
            outbound_format=dst.message_format,
        )
        dest_blocks.append(f"""\
    <connector version="{CHANNEL_VERSION}">
      <metaDataId>{i}</metaDataId>
      <name>{_esc(dst.name)}</name>
      <properties class="{dst_class}" version="{CHANNEL_VERSION}">
{dst_props_xml}
      </properties>
{dst_xform}
{_filter_block()}
      <transportName>{_esc(dst.connector_type)}</transportName>
      <mode>DESTINATION</mode>
      <enabled>true</enabled>
      <waitForPrevious>true</waitForPrevious>
    </connector>""")

    destinations_xml = "\n".join(dest_blocks)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--
  channel.xml — generated for {_esc(engine_target)}.

  Imports cleanly into NextGen Connect 4.x, Open Integration Engine
  (OIE) 4.5.x, and BridgeLink Connect 4.x. Replace every
  ``_REPLACE_WITH_*_`` sentinel before deploying.
-->
<channel version="{CHANNEL_VERSION}">
  <id>{cid}</id>
  <nextMetaDataId>{next_meta}</nextMetaDataId>
  <name>{_esc(name)}</name>
  <description>{_esc(description)}</description>
  <revision>1</revision>
  <enabled>false</enabled>
  <lastModified>
    <time>0</time>
    <timezone>UTC</timezone>
  </lastModified>
  <exportData>
    <metadata>
      <enabled>false</enabled>
      <pruningSettings>
        <pruneErroredMessages>false</pruneErroredMessages>
      </pruningSettings>
    </metadata>
  </exportData>
  <properties version="{CHANNEL_VERSION}">
    <clearGlobalChannelMap>true</clearGlobalChannelMap>
    <messageStorageMode>DEVELOPMENT</messageStorageMode>
    <encryptData>false</encryptData>
    <encryptAttachments>false</encryptAttachments>
    <encryptCustomMetaData>false</encryptCustomMetaData>
    <removeContentOnCompletion>false</removeContentOnCompletion>
    <removeOnlyFilteredOnCompletion>false</removeOnlyFilteredOnCompletion>
    <removeAttachmentsOnCompletion>false</removeAttachmentsOnCompletion>
    <initialState>STOPPED</initialState>
    <storeAttachments>true</storeAttachments>
    <metaDataColumns>
      <metaDataColumn>
        <name>SOURCE</name>
        <type>STRING</type>
        <mappingName>mirth_source</mappingName>
      </metaDataColumn>
      <metaDataColumn>
        <name>TYPE</name>
        <type>STRING</type>
        <mappingName>mirth_type</mappingName>
      </metaDataColumn>
    </metaDataColumns>
    <attachmentProperties>
      <type>None</type>
      <properties/>
    </attachmentProperties>
    <resourceIds class="linked-hash-map">
      <entry>
        <string>Default Resource</string>
        <string>[Default Resource]</string>
      </entry>
    </resourceIds>
  </properties>
  <sourceConnector version="{CHANNEL_VERSION}">
    <metaDataId>0</metaDataId>
    <name>sourceConnector</name>
    <properties class="{src_class}" version="{CHANNEL_VERSION}">
{src_props_xml}
    </properties>
{src_transformer_xml}
{_filter_block()}
    <transportName>{_esc(source.connector_type)}</transportName>
    <mode>SOURCE</mode>
    <enabled>true</enabled>
    <waitForPrevious>true</waitForPrevious>
  </sourceConnector>
  <destinationConnectors>
{destinations_xml}
  </destinationConnectors>
  <preprocessingScript>// Modify the message variable below to pre-process data before processing it&#10;return message;</preprocessingScript>
  <postprocessingScript>// This script executes once after a message has been processed&#10;return;</postprocessingScript>
  <deployScript>// This script executes once when the channel is deployed&#10;return;</deployScript>
  <undeployScript>// This script executes once when the channel is undeployed&#10;return;</undeployScript>
</channel>
"""
