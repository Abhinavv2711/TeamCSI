#include <esp_now.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SCREEN_WIDTH 128 
#define SCREEN_HEIGHT 64 
#define OLED_RESET    -1 
#define SCREEN_ADDRESS 0x3C 

#define BUZZER_PIN 23 
#define BUZZER_ON LOW
#define BUZZER_OFF HIGH

#define LED_EMPTY 19
#define LED_SLEEPING 18
#define LED_MOVING 5
#define LED_INTRUDER 17

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

String currentState = "Connecting Mesh...";

void updateDisplay(String state) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println(F("Current State:"));
  
  display.setTextSize(2);
  display.setCursor(0, 20);
  display.println(state);

  display.setTextSize(1);
  display.setCursor(0, 50);

  digitalWrite(LED_EMPTY, LOW);
  digitalWrite(LED_SLEEPING, LOW);
  digitalWrite(LED_MOVING, LOW);

  if (state == "Sleeping") {
    display.println(F("Zzz..."));
    digitalWrite(LED_SLEEPING, HIGH);
  } else if (state == "Moving") {
    display.println(F("Activity Detected"));
    digitalWrite(LED_MOVING, HIGH);
  } else if (state == "Intruder") {
    display.println(F("! ALARM !"));
  } else if (state == "Empty") {
    display.println(F("Room is Empty"));
    digitalWrite(LED_EMPTY, HIGH);
  }
  
  display.display();
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
void OnDataRecv(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {
#else
void OnDataRecv(const uint8_t *mac_addr, const uint8_t *incomingData, int len) {
#endif
  char msg[len + 1];
  memcpy(msg, incomingData, len);
  msg[len] = '\0';
  String strMsg = String(msg);

  if (strMsg.startsWith("OLED:")) {
    currentState = strMsg.substring(5);
    updateDisplay(currentState);
  }
}

void setup() {
  Serial.begin(115200);

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_OFF);
  
  pinMode(LED_EMPTY, OUTPUT);
  pinMode(LED_SLEEPING, OUTPUT);
  pinMode(LED_MOVING, OUTPUT);
  pinMode(LED_INTRUDER, OUTPUT);
  
  digitalWrite(LED_EMPTY, LOW);
  digitalWrite(LED_SLEEPING, LOW);
  digitalWrite(LED_MOVING, LOW);
  digitalWrite(LED_INTRUDER, LOW);

  if(!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("SSD1306 allocation failed"));
    for(;;);
  }

  display.clearDisplay();
  display.setTextSize(2);      
  display.setTextColor(SSD1306_WHITE); 
  display.setCursor(0, 10);
  display.println(F("WifiSense Node"));
  display.display();
  delay(2000);

  WiFi.mode(WIFI_STA);

  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  
  esp_now_register_recv_cb(OnDataRecv);
}

void loop() {
  if (currentState == "Intruder") {
    static unsigned long lastToggleTime = 0;
    static bool toggleState = false;
    
    if (millis() - lastToggleTime >= 150) {
      lastToggleTime = millis();
      toggleState = !toggleState;
      
      if (toggleState) {
        digitalWrite(BUZZER_PIN, BUZZER_ON);
        digitalWrite(LED_INTRUDER, HIGH);
      } else {
        digitalWrite(BUZZER_PIN, BUZZER_OFF);
        digitalWrite(LED_INTRUDER, LOW);
      }
    }
  } else {
    digitalWrite(BUZZER_PIN, BUZZER_OFF);
    digitalWrite(LED_INTRUDER, LOW);
  }
}
