plugins {
    bluemap.implementation
}

dependencies {
    api(project(":common"))
}

tasks.jar {
    manifest.attributes(
        "Main-Class" to "de.bluecolored.bluemap.webserver.BlueMapWebServer"
    )
}
